"""
ForgerAgent: creates and deploys a fake artifact for one predicted command.

Async context manager holding persistent SSE connections to the backend MCP server (artifact cache)
and to the forgery MCP server (injection into the honeypot). For each prediction the LLM drives a
tool-calling loop:
    1. get_artifact       look up a cached artifact for the predicted command
    2. save_artifact      store a newly generated artifact (cache miss only)
    3. deploy_artifact    write the file into the honeypot container
    4. final text answer  the artifact JSON

Guardrails: MAX_ITERATIONS caps the loop, REQUIRED_TOOLS requires at least the cache lookup.
"""

from agent_connector import AgentConnector
from agent_forger.forger_policies import PROMPT_MCP
from google.genai import types
from fastmcp import Client
import json

# Guardrails, and which MCP server serves each tool
MAX_ITERATIONS = 6
REQUIRED_TOOLS = {"get_artifact"}
BACKEND_TOOLS = {"save_artifact", "get_artifact"}
FORGERY_TOOLS = {"deploy_artifact"}

class ForgerAgent:

    def __init__(self, id, mcp_url_backend, mcp_url_forgery,  model_name, provider):
        self.id = f"AGENT FORGER-{id}"
        self.connector = AgentConnector(agent_name=self.id, provider=provider, model_name=model_name)
        self.mcp_url_backend = mcp_url_backend
        self.mcp_url_forgery = mcp_url_forgery
        self.prompt = PROMPT_MCP.format()

        # Same tools in both formats (Google FunctionDeclaration, OpenAI JSON Schema):
        # AgentConnector.create_agentic_chat() picks the one for the configured SDK
        self.google_tools, self.openai_tools = self._define_tools()

        # Set in __aenter__
        self._mcp_client_backend: Client | None = None
        self._mcp_client_forgery: Client | None = None

    # --- MCP client lifecycle ---

    async def __aenter__(self):
        """Open the persistent SSE connections to both MCP servers."""
        print(f"[{self.id}] Connecting to {self.mcp_url_backend} and {self.mcp_url_forgery}...")
        self._mcp_client_backend = Client(self.mcp_url_backend)
        await self._mcp_client_backend.__aenter__()

        self._mcp_client_forgery = Client(self.mcp_url_forgery)
        await self._mcp_client_forgery.__aenter__()
        print(f"[{self.id}] Connected.")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Close both SSE connections, also on exceptions."""
        if self._mcp_client_backend:
            print(f"[{self.id}] Closing backend connection...")
            await self._mcp_client_backend.__aexit__(exc_type, exc_val, exc_tb)
            self._mcp_client_backend = None

        if self._mcp_client_forgery:
            print(f"[{self.id}] Closing forgery connection...")
            await self._mcp_client_forgery.__aexit__(exc_type, exc_val, exc_tb)
            self._mcp_client_forgery = None

    # --- Tool definitions ---

    def _define_tools(self):
        """Return the same three tools as (google_tools, openai_tools)."""

        # Google SDK format
        get_artifact_tool = types.FunctionDeclaration(
            name="get_artifact",
            description="Searches the database for an existing fake artifact associated with the predicted command. Returns the JSON artifact if found.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "predicted_command": types.Schema(
                        type=types.Type.STRING,
                        description="The predicted attacker command to search for."
                    )
                },
                required=["predicted_command"]
            )
        )

        save_artifact_tool = types.FunctionDeclaration(
            name="save_artifact",
            description="Saves a newly generated artifact to the database, associating it with the predicted command.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "predicted_command": types.Schema(
                        type=types.Type.STRING,
                        description="The predicted attacker command."
                    ),
                    "artifact_data": types.Schema(
                        type=types.Type.OBJECT,
                        description="The artifact JSON payload containing description, intended_path, and content."
                    )
                },
                required=["predicted_command", "artifact_data"]
            )
        )

        deploy_artifact_tool = types.FunctionDeclaration(
            name="deploy_artifact",
            description="Deploys the generated fake artifact physically into the honeypot file system at the specified path.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "intended_path": types.Schema(
                        type=types.Type.STRING,
                        description="The realistic Linux path where the artifact should be created (e.g., /var/www/html/config.php)."
                    ),
                    "content": types.Schema(
                        type=types.Type.STRING,
                        description="The raw textual content of the fake file."
                    )
                },
                required=["intended_path", "content"]
            )
        )

        google_tools = [types.Tool(function_declarations=[get_artifact_tool, save_artifact_tool, deploy_artifact_tool])]

        # OpenAI / OpenRouter / local format (JSON Schema)
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_artifact",
                    "description": "Searches the database for an existing fake artifact associated with the predicted command. Returns the JSON artifact if found.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "predicted_command": {
                                "type": "string",
                                "description": "The predicted attacker command to search for."
                            }
                        },
                        "required": ["predicted_command"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "save_artifact",
                    "description": "Saves a newly generated artifact to the database, associating it with the predicted command.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "predicted_command": {
                                "type": "string",
                                "description": "The predicted attacker command."
                            },
                            "artifact_data": {
                                "type": "object",
                                "description": "The artifact JSON payload containing description, intended_path, and content."
                            }
                        },
                        "required": ["predicted_command", "artifact_data"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "deploy_artifact",
                    "description": "Deploys the generated fake artifact physically into the honeypot file system at the specified path.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "intended_path": {
                                "type": "string",
                                "description": "The realistic Linux path where the artifact should be created (e.g., /var/www/html/config.php)."
                            },
                            "content": {
                                "type": "string",
                                "description": "The raw textual content of the fake file."
                            }
                        },
                        "required": ["intended_path", "content"]
                    }
                }
            }

        ]

        return google_tools, openai_tools

    # --- Artifact forging ---

    async def decide(self, predicted_cmd, eventCommand):

        if not self._mcp_client_backend:
            raise RuntimeError(
                f"[{self.id}] Backend MCP client not initialized."
            )

        if not self._mcp_client_forgery:
            raise RuntimeError(
                f"[{self.id}] Forgery MCP client not initialized."
            )

        print(f"\n[{self.id}] Starting loop for '{predicted_cmd}' (session {eventCommand.session_id})...")

        # 1. Chat session with the wrapper for the configured SDK
        chat = self.connector.create_agentic_chat(
            system_instruction=self.prompt,
            google_tools=self.google_tools,
            openai_tools=self.openai_tools
        )

        # 2. Initial message
        initial_message = (
            "New event received.\n"
            f"Session ID: {eventCommand.session_id}\n"
            f"Attacker command: {eventCommand.cmd}\n"
            f"Predicted next command: {predicted_cmd}\n"
        )
        response = await chat.send_message(initial_message)

        # No tool calls at all: the workflow failed
        if not response.function_calls:
            print(f"[{self.id}] WARNING: LLM answered without calling tools: {response.text}")
            return []

        # 3. Agentic loop: run the requested tools on the right MCP server and send back the
        #    results until the LLM answers in plain text (the artifact JSON).

        called_tools = set()
        iteration = 0

        while response.function_calls and iteration < MAX_ITERATIONS:
            iteration += 1
            tool_responses = []

            for function_call in response.function_calls:
                func_name = function_call.name
                args = function_call.args
                call_id = function_call.id  # None for Google, string for OpenAI

                print(f"[{self.id}] Iteration {iteration}: calling '{func_name}'")
                called_tools.add(func_name)

                if func_name in BACKEND_TOOLS:
                    endpoint = "backend"
                else:
                    endpoint = "forgery"

                try:
                    tool_result = await self._execute_mcp_call(func_name, args, endpoint)
                except Exception as e:
                    print(f"[{self.id}] ERROR: tool '{func_name}' failed: {e}")
                    tool_result = {"error": str(e)}

                # Tool result in the format of the active provider
                formatted_response = self.connector.format_tool_response(func_name, tool_result, call_id)
                tool_responses.append(formatted_response)

            response = await chat.send_message(tool_responses)

        # Guardrails
        if iteration >= MAX_ITERATIONS:
            print(f"[{self.id}] WARNING: MAX_ITERATIONS ({MAX_ITERATIONS}) reached, loop stopped.")

        missing_tools = REQUIRED_TOOLS - called_tools
        if missing_tools:
            print(f"[{self.id}] WARNING: required tools not called {missing_tools}, artifact discarded.")
            return []

        # 4. Parse the artifact JSON, stripping any markdown fences
        raw_response = response.text
        if not raw_response:
            print(f"[{self.id}] WARNING: empty final answer, no artifact.")
            return []

        raw_response = raw_response.replace("```json", "").replace("```", "").strip()

        try:
            parsed = json.loads(raw_response)
            return parsed
        except Exception:
            print(f"[{self.id}] WARNING: invalid artifact JSON:\n{raw_response}")
            return []

    async def _execute_mcp_call(self, tool_name: str, args: dict, endpoint: str):
        """Call an MCP tool on the given server over its persistent SSE connection."""
        if endpoint == "forgery":
            result = await self._mcp_client_forgery.call_tool(tool_name, args)
        else:
            result = await self._mcp_client_backend.call_tool(tool_name, args)
        return str(result)
