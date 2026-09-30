# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An adaptive cyber-deception framework: an SSH honeypot instrumented with a multi-agent LLM pipeline that **predicts an attacker's next command** and **proactively plants fake-but-credible files** in the honeypot's filesystem before the attacker looks for them. Evolution of a prior monolithic thesis project (`Predictive_deception`) into 4 Docker containers coordinated over MCP (Model Context Protocol / SSE), plus an on-demand 5th container that drives an autonomous attacker LLM against the honeypot for end-to-end testing.

## Running the system

There is no local dev server / test suite — this is a Docker Compose multi-container system, run and debugged via Docker.

```bash
# Before first run: fix bind-mount paths in docker-compose.yml under `backend.volumes`
# (source: paths to a local chroma_storage / sessions / artifacts directory)
# and create ./.env with:
#   LLM_API_KEY=<key>
#   LLM_SDK=google|openai|openrouter

docker-compose up --build -d
docker compose logs -f <honeypot|agentic-system|backend|mcp-forgery>
docker-compose down

ssh honeypot@localhost -p 2222   # password: password123

# Attacker agent (on-demand, not started by a plain `up`): see "Attacker agent" section below
docker compose --profile attacker up --build attacker
docker compose logs -f attacker
```

The `backend` container requires a **pre-populated ChromaDB collection named `honeypot_attacks`** at the bind-mounted `vector_db` path (embedding function `all-MiniLM-L6-v2`) — there is no indexing script in this repo; it must be produced externally (see the original `Predictive_deception` project) and mounted in.

Iterating on a single container: `docker-compose up --build -d <service>` then tail its logs. There's no hot reload — Python files are baked into the image at build time (`COPY . .`).

## Architecture

Four always-on containers plus an on-demand 5th (`attacker`, Compose profile-gated), two Docker networks:

- **`honeypot_net`**: honeypot ↔ agentic-system ↔ mcp-forgery (the "operational" deception network)
- **`backend_net`**: agentic-system ↔ backend (isolates historical/session data)

```
attacker --SSH:2222--> honeypot --HTTP POST /new_command--> agentic-system
                                                                 |  SSE/MCP
                                                    +------------+------------+
                                                    |                         |
                                                 backend                 mcp-forgery
                                          (ChromaDB + JSONL logs)   (Docker API -> honeypot)
```

1. **`honeypotContainer/fakeshell.py`** — set as the login shell for the `honeypot` OS user. For every command typed: fire-and-forget POST to `agentic-system` (100ms timeout, swallows failures — the shell must never hang or reveal the deception), then executes the command **for real** via `pty.fork()` + `execve("/bin/bash", ...)` so output is authentic.

2. **`agentContainer/`** — the orchestrator. FastAPI app ([agentArchitecture/honeypot_listener.py](agentContainer/agentArchitecture/honeypot_listener.py)) exposing `POST /new_command`. Uses FastAPI `lifespan` to open all SSE connections once at startup (the `HoneypotListener` itself is an async context manager whose `__aenter__`/`__aexit__` cascade into each agent's) and store the instance in `app.state.orch` — avoids reconnecting per-request and avoids globals.
   - **Dispatch flow** (`HoneypotListener.dispatch`): Phase 1 runs the single `PredictiveAgent.decide(event)` to get up to `k` predicted commands. Phase 2 fans out one `ForgerAgent.decide(cmd, event)` per prediction via `asyncio.create_task` + `asyncio.wait(timeout=180)` — stragglers past 3 minutes are cancelled, not awaited.
   - A pool of `NUM_PREDICTION` `ForgerAgent` instances is pre-created (one per possible prediction slot), each with its own two SSE connections, so forgery deployment is parallel.

3. **Agent core** (`agentContainer/agentArchitecture/`):
   - [`agent_predictive/predictive_core.py`](agentContainer/agentArchitecture/agent_predictive/predictive_core.py) — `PredictiveAgent`: agentic tool-calling loop that must call `log_session_event` → `get_session_history` → `retrieve` (RAG over ChromaDB) before accepting the LLM's final plain-text prediction. Guardrails: `MAX_ITERATIONS = 4`, and `REQUIRED_TOOLS` must all have been called or the prediction is discarded (returns `[]`, skipping Phase 2 entirely). Attacker-controlled data is wrapped in `<untrusted_data>` tags in the prompt to blunt prompt injection.
   - [`agent_forger/forger_core.py`](agentContainer/agentArchitecture/agent_forger/forger_core.py) — `ForgerAgent`: given one predicted command, loop calls `get_artifact` (backend, required) → optionally `save_artifact` (backend) → `deploy_artifact` (mcp-forgery). `MAX_ITERATIONS = 6`. Final LLM output is parsed as JSON (`{description, intended_path, content}`); malformed JSON is discarded silently.
   - [`agent_connector.py`](agentContainer/agentArchitecture/agent_connector.py) + [`adapter_connector.py`](agentContainer/agentArchitecture/adapter_connector.py) — the multi-provider abstraction every agent goes through. `AgentConnector.create_agentic_chat()` picks `GoogleChatWrapper` or `OpenAIChatWrapper` based on the configured SDK; both expose the same `send_message(message) -> UnifiedResponse(text, function_calls)` interface, hiding that Google's `Part.from_function_response` needs no `call_id` while OpenAI's tool-result messages require `tool_call_id`. **When adding/changing a tool or provider, this is the seam to touch — agent core code (`predictive_core.py`/`forger_core.py`) must never branch on provider.**
   - Each agent defines its tools **twice** — once as `google.genai.types.FunctionDeclaration` (`google_tools`), once as raw JSON Schema dicts (`openai_tools`) — because the two SDKs take incompatible tool-definition formats. Keep both in sync when changing a tool's signature.

4. **`backendContainer/mcp_server.py`** — one `FastMCP` server exposing 5 tools over SSE on `:8000`, backed by three bind-mounted volumes: ChromaDB at `/app/data/vector_db` (RAG via `retrieve`), per-session JSONL at `/app/data/sessions/session_<id>.jsonl` (`log_session_event`/`get_session_history`), and append-only `/app/data/artifacts/artifacts.jsonl` (`save_artifact`/`get_artifact`, latest-match-wins on lookup — no dedup/rotation).

5. **`mcpForgeryContainer/mcp_server.py`** — single tool `deploy_artifact(intended_path, content)`. Talks to `/var/run/docker.sock` (mounted in) and calls `container.put_archive()` against the hardcoded container name `agenticpredictivedeception-honeypot-1` — an in-memory tar equivalent to `docker cp`, so the honeypot never needs to be modified or restarted to receive new files. **The hardcoded container name is coupled to the Compose project name** (`docker-compose.yml`'s directory-derived prefix) — renaming the project directory or the `honeypot` service breaks this.

6. **`attackerContainer/`** — an on-demand attacker LLM that drives a real SSH session against the honeypot to exercise the whole prediction→forgery pipeline end-to-end. Not part of the always-on stack: gated behind Compose `profiles: ["attacker"]` and has no `restart:` policy (it's a one-shot session, not a server).
   - [`ssh_shell.py`](attackerContainer/ssh_shell.py) — `SSHShellSession`: opens one persistent paramiko `invoke_shell()` channel (fakeshell keeps `cwd` as in-process state across the whole login session, so one exec per command wouldn't work). Turn-completion is detected by matching fakeshell's exact ANSI prompt format at the end of the accumulated read buffer (`PROMPT_RE`) rather than an injected shell sentinel — a sentinel like `; echo __DONE__` silently fails on `cd`, since fakeshell intercepts `cmd.startswith("cd")` before ever spawning bash. The regex approach handles `cd`/errors/normal commands uniformly. Includes hang recovery (Ctrl-C + short second timeout) for accidental interactive/pager commands, and EOF detection for `exit`/`quit`/`logout`.
   - [`attacker_core.py`](attackerContainer/attacker_core.py) — `AttackerAgent`: async context manager (opens/closes the SSH session) whose `run()` loop is a plain multi-turn chat via `AgentConnector.create_agentic_chat(..., google_tools=[], openai_tools=[])` — no MCP tools at all, unlike the other three agents. Each turn wraps the previous command's real output in `<shell_output>` tags (same anti-prompt-injection convention as `<untrusted_data>` in `predictive_core.py`, defending against a forged file's content trying to instruct the attacker LLM itself). Stops on the model replying `STOP: <reason>`, on the target closing the connection, or on hitting `MAX_TURNS` (guardrail, like `MAX_ITERATIONS` elsewhere). Per-command output is truncated (~4000 chars) before being fed back to the LLM — real shell output has no such cap anywhere else in the codebase since other agents only ever pass around small JSON tool results.
   - [`attacker_policies.py`](attackerContainer/attacker_policies.py) — persona/system prompt: adaptive pentester (recon → escalation → sensitive-data hunting), explicit authorized-testing framing, strict one-command-per-turn contract, list of interactive commands to avoid (`top`, `less`, `vim`, `man`, `watch`, ...).
   - **Build context quirk**: unlike the other 4 services, `attacker`'s Compose `context` is the **repo root** (`dockerfile: attackerContainer/Dockerfile`), so its Dockerfile can `COPY agentContainer/agentArchitecture/agent_connector.py agentContainer/agentArchitecture/adapter_connector.py` — reusing the multi-provider LLM abstraction instead of duplicating it. If `agent_connector.py`/`adapter_connector.py` change, the attacker image just needs a rebuild to pick it up; there's nothing else to keep in sync.
   - Transcripts are JSONL, one line per turn/event, written to `attackerContainer/transcripts/` (bind-mounted, gitignored) — inspect these after a run to see the full command/output history and how the session ended.

## Configuration

Runtime knobs are container env vars in `docker-compose.yml` (`agentic-system` service): `PROVIDER` (`cloud`|`local`), `MODEL_NAME`, `BACKEND_MCP_URL`, `FORGERY_MCP_URL`, `NUM_PREDICTION` (= size of the forger pool = max predictions per event). The `attacker` service has its own analogous `PROVIDER`/`MODEL_NAME` plus `HONEYPOT_HOST`/`HONEYPOT_PORT`/`HONEYPOT_USER`/`HONEYPOT_PASSWORD` and `MAX_TURNS`.

LLM credentials are never an env var in cleartext — they're mounted as a Docker secret from `./.env` to `/run/secrets/llm_config_secret` and parsed by `AgentConnector._load_key_logic()`. `PROVIDER=local` bypasses the secret entirely and talks to LM Studio at `host.docker.internal:1234`.

## Known rough edges (from `todo.txt`, still open)

- `fakeshell.py` currently leaks tells that reveal the honeypot (e.g. the orchestrator POST, the shell binary being `fakeshell.py`) — not yet hardened.
- ForgerAgent behavior/prompting is flagged as needing improvement.
- The attacker agent's SSH turn-detection (`PROMPT_RE` in `ssh_shell.py`) is tightly coupled to `fakeshell.py`'s exact ANSI prompt string — if that format ever changes, the regex needs updating too.
