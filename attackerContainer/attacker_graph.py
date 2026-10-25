"""
attacker_graph.py — LLM attacker against the honeypot, orchestrated with LangGraph.

Four-node graph:

        ┌──────────┐     ┌─────────┐   run_command   ┌─────────┐
START ─►│ open_ssh │ ──► │  agent  │ ───────────────►│ execute │
        └──────────┘     │  (LLM)  │◄────────────────┤ (shell) │
                         └────┬────┘     output       └─────────┘
                              │ finish / MAX_STEPS
                              ▼
                         ┌─────────┐
                         │  close  │ ──► END
                         └─────────┘

- open_ssh : open the SSH connection and read the initial banner.
- agent    : ask the LLM for the next command (run_command tool) or to stop (finish tool).
- execute  : send the command to the shell and read its output, then go back to the agent.
- close    : end the session (close the channel, write the summary to the transcript).

The loop is agent -> execute -> agent. It exits to 'close' when the LLM calls finish or when
MAX_STEPS (the maximum number of executed commands) is reached.
"""

import asyncio
import json
import os
import time
from typing import Annotated, TypedDict

from attacker_policies import PROMPT_ATTACKER
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from ssh_shell import SSHShellSession

# Secret with the attacker's LLM key; overridable so it can differ from the defense key
SECRET_FILE = os.getenv("LLM_SECRET_FILE", "/run/secrets/llm_config_secret")
MAX_OUTPUT_CHARS = 4000


# --- Tools exposed to the LLM --------------------------------------------------------------------
# The bodies are never run: they exist only to generate the LLM schema (bind_tools).
# The real command execution happens in the 'execute' node, which drives the shell directly.

@tool
def run_command(command: str) -> str:
    """Run exactly one shell command on the target and return its real terminal output and cwd."""
    return ""

@tool
def finish(reason: str) -> str:
    """End the session when the objective is met or nothing new would surface."""
    return ""


# --- Graph state ---------------------------------------------------------------------------------

class AttackState(TypedDict):
    """State shared by the nodes. 'messages' uses add_messages, which APPENDS new messages."""
    messages: Annotated[list, add_messages]
    step: int           # commands executed so far
    finished: bool      # the LLM called finish


# --- LLM configuration (same Docker secret as the other agents) -----------------------------------

def _load_llm_config():
    """Read LLM_API_KEY and LLM_SDK from the Docker secret (.env format)."""
    if not os.path.exists(SECRET_FILE):
        raise ValueError(f"[ATTACKER] Secret '{SECRET_FILE}' not found.")
    config = {}
    with open(SECRET_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip()
    api_key = config.get("LLM_API_KEY")
    sdk = config.get("LLM_SDK", "").lower()
    if not api_key or not sdk:
        raise ValueError(f"[ATTACKER] LLM_API_KEY/LLM_SDK missing in '{SECRET_FILE}'.")
    return api_key, sdk


def _build_model(model_name, provider):
    """Build the LangChain chat model for the configured provider/SDK and bind the tools.

    google             -> ChatGoogleGenerativeAI
    openai/openrouter  -> ChatOpenAI (only base_url changes)
    local (LM Studio)  -> ChatOpenAI towards host.docker.internal
    """
    # max_tokens caps the output per request: avoids OpenRouter's 402 (which otherwise reserves
    # the model's full output) and keeps costs in check.
    MAX_TOKENS = 2048

    if provider == "local":
        from langchain_openai import ChatOpenAI
        model = ChatOpenAI(
            model=model_name,
            temperature=0.0,
            api_key="lm-studio",
            base_url="http://host.docker.internal:1234/v1",
            max_tokens=MAX_TOKENS
        )
        return model.bind_tools([run_command, finish])

    api_key, sdk = _load_llm_config()

    if sdk == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI
        # Safety filters off: authorized security testing, so the model does not refuse mid-session.
        try:
            from langchain_google_genai import HarmBlockThreshold, HarmCategory
            safety = {
                HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
            }
        except Exception:
            safety = None
        model = ChatGoogleGenerativeAI(
            model=model_name,
            google_api_key=api_key,
            temperature=0.0,
            safety_settings=safety,
            max_retries=3,
            max_output_tokens=MAX_TOKENS
        )
        return model.bind_tools([run_command, finish])

    from langchain_openai import ChatOpenAI
    base_url = "https://openrouter.ai/api/v1" if sdk == "openrouter" else "https://api.openai.com/v1"
    model = ChatOpenAI(model=model_name,
        temperature=0.0,
        api_key=api_key,
        base_url=base_url,
        max_retries=3,
        max_tokens=MAX_TOKENS
    )
    return model.bind_tools([run_command, finish])


def _truncate(text):
    text = text or ""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n[...truncated, {len(text) - MAX_OUTPUT_CHARS} more characters...]"


# --- Agent ---------------------------------------------------------------------------------------

class AttackerGraphAgent:
    """LLM attacker. Async context manager: owns the SSH channel and the transcript file,
    opened once and closed reliably."""

    def __init__(self, shell: SSHShellSession, model_name, provider, max_turns, transcript_path):
        self.id = "AGENT ATTACKER"
        self.shell = shell
        self.max_steps = int(max_turns)
        self.transcript_path = transcript_path
        self.model_name = model_name
        self.provider = provider

        self._model = None
        self._transcript_file = None
        self._graph = None

    # --- lifecycle ---

    async def __aenter__(self):
        self._model = _build_model(self.model_name, self.provider)
        self._transcript_file = open(self.transcript_path, "a", encoding="utf-8")
        self._graph = self._build_graph()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._transcript_file:
            self._transcript_file.close()
            self._transcript_file = None

    # --- graph ---

    def _build_graph(self):

        async def open_ssh(state: AttackState):
            """Open the SSH connection and read the initial banner."""
            print(f"[{self.id}] Connecting to the honeypot over SSH...")
            await asyncio.to_thread(self.shell.connect)
            banner = await asyncio.to_thread(self.shell.drain_banner)
            print(f"[{self.id}] Connected ({len(banner.output)} banner chars).")
            self._log({"event": "session_start", "banner": _truncate(banner.output)})
            # Show the banner to the LLM and ask for the first command
            kickoff = (
                "Connection established. You have an interactive shell on the target.\n"
                f"<shell_output>\n{_truncate(banner.output)}\n</shell_output>\n"
                "Propose the first command to run by calling run_command."
            )
            return {"messages": [("user", kickoff)]}

        async def agent(state: AttackState):
            """Ask the LLM for the next command (run_command) or to stop (finish)."""
            # Budget spent: skip a useless LLM call; route() will send to 'close'.
            if state["step"] >= self.max_steps:
                return {}
            response = await self._model.ainvoke(state["messages"])
            return {"messages": [response]}

        async def execute(state: AttackState):
            """Send the command to the shell and read its output. Answer every tool call."""
            last = state["messages"][-1]
            tool_msgs = []
            step = state["step"]

            for call in last.tool_calls:
                tcid = call["id"]
                command = (call.get("args", {}) or {}).get("command", "").strip()
                if not command:
                    tool_msgs.append(ToolMessage(content="ERROR: empty command.", tool_call_id=tcid))
                    continue

                step += 1
                print(f"[{self.id}] step {step}: {command}")
                result = await asyncio.to_thread(self.shell.run_command, command)
                self._log({"event": "turn",
                    "step": step,
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "command": command,
                    "output": _truncate(result.output),
                    "cwd": result.cwd, "timed_out": result.timed_out}
                )

                if result.eof:
                    self._log({"event": "session_closed_by_target"})
                    tool_msgs.append(ToolMessage(
                        content="The target closed the connection. The session is over.",
                        tool_call_id=tcid))
                    continue

                text = f"<shell_output>\n{_truncate(result.output)}\n</shell_output>\ncwd: {result.cwd}"
                if result.timed_out:
                    text += "\n[note: command interrupted by timeout with Ctrl-C]"
                tool_msgs.append(ToolMessage(content=text, tool_call_id=tcid))

            return {"messages": tool_msgs, "step": step}

        async def close(state: AttackState):
            """End the session: close the SSH channel and write the summary."""
            report = None
            last = state["messages"][-1]
            # If we exit because of finish, recover the reason from the tool call
            if isinstance(last, AIMessage) and last.tool_calls:
                for call in last.tool_calls:
                    if call["name"] == "finish":
                        report = (call.get("args", {}) or {}).get("reason")
            self._log({"event": "session_summary", "steps": state["step"], "finish_report": report})
            await asyncio.to_thread(self.shell.close)
            print(f"[{self.id}] SSH session closed after {state['step']} steps.")
            return {"finished": True}

        def route(state: AttackState):
            """After 'agent': run the command, or close (finish / MAX_STEPS / no tool call)."""
            if state["step"] >= self.max_steps:
                print(f"[{self.id}] Reached MAX_STEPS ({self.max_steps}).")
                return "close"
            last = state["messages"][-1]
            if not (isinstance(last, AIMessage) and last.tool_calls):
                return "close"
            names = {c["name"] for c in last.tool_calls}
            if "finish" in names:
                return "close"
            if "run_command" in names:
                return "execute"
            return "close"

        graph = StateGraph(AttackState)
        graph.add_node("open_ssh", open_ssh)
        graph.add_node("agent", agent)
        graph.add_node("execute", execute)
        graph.add_node("close", close)

        graph.add_edge(START, "open_ssh")
        graph.add_edge("open_ssh", "agent")
        graph.add_conditional_edges("agent", route, {"execute": "execute", "close": "close"})
        graph.add_edge("execute", "agent")
        graph.add_edge("close", END)
        return graph.compile()

    # --- run ---

    async def run(self):
        initial_state = {
            "messages": [("system", PROMPT_ATTACKER)],
            "step": 0,
            "finished": False,
        }
        # Each step is ~2 graph steps (agent + execute); add margin for open_ssh/close.
        config = {"recursion_limit": self.max_steps * 3 + 10}
        try:
            await self._graph.ainvoke(initial_state, config=config)
        except Exception as e:
            # Hitting LangGraph's recursion_limit raises: treat it as the end of the session.
            self._log({"event": "graph_stopped", "reason": str(e)})
            print(f"[{self.id}] Graph stopped: {e}")
            try:
                await asyncio.to_thread(self.shell.close)
            except Exception:  # noqa: S110
                pass

    # --- utils ---

    def _log(self, event):
        if self._transcript_file:
            self._transcript_file.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._transcript_file.flush()
