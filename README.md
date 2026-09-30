# 🕵️ Agentic Predictive Deception

> An AI-based cybersecurity framework that combines honeypot technology with a multi-LLM agentic system to **predict, prepare and inject deceptive artifacts** in response to attacker behavior in real time.

---

## Table of Contents

- [Overview](#overview)
- [System Architecture](#system-architecture)
- [Components and How They Work](#components-and-how-they-work)
  - [1. Honeypot Container](#1-honeypot-container)
  - [2. Agentic System Container](#2-agentic-system-container)
  - [3. Backend Container](#3-backend-container)
  - [4. MCP Forgery Container](#4-mcp-forgery-container)
  - [5. Attacker Container](#5-attacker-container)
- [End-to-End Execution Flow](#end-to-end-execution-flow)
- [Internal Agentic Architecture](#internal-agentic-architecture)
  - [AgentConnector](#agentconnector)
  - [PredictiveAgent](#predictiveagent)
  - [ForgerAgent](#forgeragent)
  - [HoneypotListener (Orchestrator)](#honeypotlistener-orchestrator)
  - [AttackerAgent](#attackeragent)
- [Project Structure](#project-structure)
- [Configuration and Startup](#configuration-and-startup)
- [Key Design Points](#key-design-points)
- [Research Context](#research-context)

---

## Overview

**agenticPredictiveDeception** is an adaptive cyber deception system that goes beyond classic static honeypots. When an attacker connects via SSH and types a command, the system does not simply log it: it triggers a multi-agent AI pipeline that **predicts the attacker's next command** and **proactively prepares fake but credible files** that are already present in the honeypot filesystem before they are looked for.

The result is a dynamic deceptive environment, able to adapt to the specific behavior of each attacker in real time.

**agenticPredictiveDeception** is the agentic evolution of [**Predictive Deception: LLM-based Command Anticipation in SSH Honeypots**](https://github.com/BlackRaffo70/Predictive_deception), a project developed as part of the Master's Degree in Computer Engineering at the **University of Bologna**.

The original project laid the conceptual and technical foundations of the system:

- an SSH honeypot with a realistic **fakeshell**
- a **RAG + LLM** prediction engine evaluated on real attack datasets (CyberLab Honeynet via Zenodo)
- a **Defender** module that predicts the next commands and generates artifacts in the VM filesystem

This repository takes that architecture and turns it into a **containerized multi-agent system**, introducing:

| Aspect | Original project | This extension |
|---|---|---|
| Deployment | Vagrant VM + Ansible | Docker Compose (4 containers + on-demand attacker) |
| Coordination | Monolithic script (`defender.py`) | Asynchronous multi-agent pipeline (PredictiveAgent + pool of ForgerAgents) |
| LLM↔tool communication | Direct in-process calls | MCP protocol over SSE (FastMCP) |
| Artifact injection | File writes in the VM | Docker API (`put_archive`) on the honeypot container |
| LLM provider | Gemini / Ollama (local) | Abstracted: Google, OpenAI, OpenRouter, LM Studio |
| Forgery parallelism | Sequential | `k` ForgerAgents in parallel, one per prediction |
| Testing | Manual (human over SSH) | Autonomous LLM-driven AttackerAgent |

The ChromaDB vector database and the historical attack corpus from the previous project are reused directly as a bind volume of the `backend` container.

## System Architecture

The system is made up of **four Docker containers** (plus an optional fifth, on-demand **attacker** container) that communicate over two separate internal networks:

```
┌──────────────────────────────────────────────┐
│     ATTACKER (human, or attacker container)  │
│              (SSH on port 2222)              │
└───────────────────┬──────────────────────────┘
                    │ SSH
        ┌───────────▼────────────┐
        │        honeypot        │  (honeypot_net)
        │       Port: 2222       │  fakeshell.py
        └───────────┬────────────┘
                    │ HTTP POST /new_command        ▲
                    │                               │ docker cp
        ┌───────────▼────────────┐      ┌───────────┴──────────┐
        │    agentic-system      │─────▶│     mcp-forgery      │
        │  (honeypot_net +       │ SSE  │    (honeypot_net)    │
        │   backend_net)         │      │     Docker API       │
        │  FastAPI + LLM agents  │      └──────────────────────┘
        └───────────┬────────────┘
                    │ SSE (MCP)
        ┌───────────▼────────────┐
        │        backend         │  (backend_net)
        │    ChromaDB + Logs     │
        └────────────────────────┘
```

**Docker networks:**
- `honeypot_net`: connects honeypot, agentic-system, mcp-forgery and attacker. It is the "operational" deception network.
- `backend_net`: connects agentic-system and backend. It isolates the historical data (RAG, sessions, artifacts).

---

## Components and How They Work

### 1. Honeypot Container

**Directory:** `honeypotContainer/`  
**Base image:** Python 3.10 + OpenSSH Server

The container exposes a realistic SSH server on port `2222`. When an attacker connects, they are presented with a simulated Ubuntu 22.04 shell. In fact, the `honeypot` user has `fakeshell.py` directly as its login shell.

**`fakeshell.py`** is the heart of the honeypot. For every command typed by the attacker, it:

1. Generates a unique `SESSION_ID` in the format `YYYY-MM-DD_IP`.
2. Calls `trigger_ai()` in a non-blocking way to notify the agentic container that reasoning should start.
3. Executes the command **for real** inside the container, showing the authentic system output.

The key point is that the shell runs real commands on the container (a reduced but working Linux system), making the attacker's experience authentic, while the AI works in the background to prepare the files the attacker will find when running the next command (if it is one of the predicted commands).

---

### 2. Agentic System Container

**Directory:** `agentContainer/`  
**Base image:** Python 3.10 slim  
**Port:** 8000 (HTTP/FastAPI)

It is the brain of the system. It exposes a FastAPI endpoint `POST /new_command` that receives events from the fakeshell and coordinates the work of the two types of AI agents.

The container is configured through environment variables:

| Variable | Description |
|-----------|-------------|
| `PROVIDER` | `cloud` (reads the API key from a secret) or `local` (LM Studio) |
| `MODEL_NAME` | Name of the LLM model (e.g. `deepseek/deepseek-v4-flash`) |
| `BACKEND_MCP_URL` | SSE URL of the backend (`http://backend:8000/sse`) |
| `FORGERY_MCP_URL` | SSE URL of the forgery (`http://mcp-forgery:8000/sse`) |
| `NUM_PREDICTION` | Number `k` of commands to predict for each event |

The API key and the SDK are passed via a **Docker secret** (the `.env` file mounted at `/run/secrets/llm_config_secret`), never as plain-text environment variables.

---

### 3. Backend Container

**Directory:** `backendContainer/`  
**Base image:** Python 3.10 slim  
**Port:** 8000 (MCP/SSE)

It exposes an MCP (Model Context Protocol) server that acts as the persistence and memory layer for the agents. It manages three distinct resources mounted as volumes:

- **Vector DB** (`/app/data/vector_db`): ChromaDB database with `all-MiniLM-L6-v2` embeddings containing historical attacks indexed for vector search (RAG).
- **Sessions** (`/app/data/sessions`): one JSONL file per session (built from attacker IP + day), with the historical sequence of commands.
- **Artifacts** (`/app/data/artifacts`): JSONL file with the deception artifacts already generated, indexed by predicted command.

It exposes the following MCP tools:

| Tool | Used by | Function |
|------|----------|----------|
| `log_session_event` | PredictiveAgent | Appends the new command to the session file |
| `get_session_history` | PredictiveAgent | Retrieves the last N commands of the session |
| `retrieve` | PredictiveAgent | Vector query on ChromaDB to find similar attacks (RAG) |
| `get_artifact` | ForgerAgent | Checks whether an artifact already exists for the predicted command |
| `save_artifact` | ForgerAgent | Saves a new artifact in the JSONL file |

---

### 4. MCP Forgery Container

**Directory:** `mcpForgeryContainer/`  
**Base image:** Python 3.10 slim  
**Port:** 8000 (MCP/SSE)  
**Special privilege:** access to the Docker socket (`/var/run/docker.sock`)

It is the "operational arm" of the system. It exposes a single MCP tool:

**`deploy_artifact(intended_path, content)`**: physically injects a file into the honeypot container's filesystem without the honeypot having to be modified or restarted. It does so by building a TAR archive in RAM and pushing it through the Docker API (`put_archive`), equivalent to `docker cp`. The file appears in the honeypot as if it had always been there.

---

### 5. Attacker Container

**Directory:** `attackerContainer/`  
**Base image:** Python 3.10 slim  
**Compose profile:** `attacker` (on-demand, it does not start with a plain `docker-compose up`)

Until now the system could only be tested **manually**, by connecting via SSH and typing commands by hand. The attacker container automates this: it runs an LLM-driven **AttackerAgent** that connects to the honeypot over real SSH, exactly like an external attacker, explores autonomously and reacts adaptively to what it observes in the shell — including the artifacts that the ForgerAgent plants dynamically during the session. This makes it possible to validate the full prediction → forgery → attacker reaction loop in a repeatable way.

The container reuses the multi-provider LLM abstraction of the agentic system (`agent_connector.py` and `adapter_connector.py` are copied from `agentContainer/` at build time, so the build context is the repository root). It is configured through environment variables:

| Variable | Description |
|-----------|-------------|
| `HONEYPOT_HOST` / `HONEYPOT_PORT` | SSH target (`honeypot:2222`) |
| `HONEYPOT_USER` / `HONEYPOT_PASSWORD` | SSH credentials (`honeypot` / `password123`) |
| `PROVIDER` | `cloud` or `local`, as for the agentic system |
| `MODEL_NAME` | Name of the LLM model driving the attacker |
| `MAX_TURNS` | Maximum number of commands per session (default `25`) |

Each session produces a structured **JSONL transcript** (`session_start`, one `turn` event per command with output and cwd, and the termination reason), saved in `attackerContainer/transcripts/` on the host.

---

## End-to-End Execution Flow

```
Attacker types: "cat /etc/passwd"
        │
        ▼
[fakeshell] trigger_ai() → HTTP POST {session_id, cmd, ip, cwd, ...}
        │
        ▼
[HoneypotListener] dispatch(event)
        │
        ├─── PHASE 1: PREDICTION ────────────────────────────────────────
        │    [PredictiveAgent]
        │    ├─ Tool: log_session_event   → saves "cat /etc/passwd" in the log
        │    ├─ Tool: get_session_history → retrieves the last 5 commands of the session
        │    ├─ Tool: retrieve            → RAG on ChromaDB (similar attacks)
        │    └─ LLM generates prediction: ["ls -la /etc", "cat /etc/shadow", "id", ...]
        │
        └─── PHASE 2: FORGERY (parallel, one ForgerAgent per prediction) ─
             [ForgerAgent-0] for "ls -la /etc"
             ├─ Tool: get_artifact("ls -la /etc") → not found
             ├─ LLM generates JSON artifact ({path, content})
             ├─ Tool: save_artifact(...)  → saves it in the JSONL artifact database
             └─ Tool: deploy_artifact(intended_path, content)
                  └─ [mcp-forgery] Docker API → in-memory TAR → honeypot filesystem
             
             [ForgerAgent-1] for "cat /etc/shadow"  ← in parallel
             ...

When the attacker runs "ls -la /etc", the fake files are already there.
```

---

## Internal Agentic Architecture

### AgentConnector

**File:** `agentContainer/agentArchitecture/agent_connector.py`

It abstracts communication with the LLM model, transparently supporting several providers:

- **Google Gemini** (SDK `google-genai`)
- **OpenAI** (SDK `openai`)
- **OpenRouter** (SDK `openai` with an alternative base_url)
- **Local** (LM Studio via OpenAI-compat, `host.docker.internal:1234`)

The provider and the key are read from the Docker secret. The main method is `create_agentic_chat()`, which returns a chat session configured with a system prompt and tool definitions, wrapped in an object with a unified interface.

The unified wrapper (`adapter_connector.py`) hides the structural differences between the SDKs:
- Google does not require a `call_id` in tool results; OpenAI does.
- Google uses `FunctionDeclaration`; OpenAI uses JSON Schema.
- Google manages the history internally; OpenAI requires it to be passed explicitly.

### PredictiveAgent

**File:** `agentContainer/agentArchitecture/agent_predictive/predictive_core.py`

An autonomous agent that implements the RAG + prediction cycle. It is an **async context manager**: it opens a persistent SSE connection to the backend MCP at startup and reuses it for every request, avoiding reconnection overhead.

The `decide(event)` method starts an agentic loop:
1. It sends the command as the initial message (isolated in `<untrusted_data>` tags to prevent prompt injection).
2. The LLM autonomously calls the tools in the correct order.
3. The loop stops when the LLM produces plain text (the final prediction).

Two safety guardrails:
- `MAX_ITERATIONS = 4`: prevents infinite loops.
- `REQUIRED_TOOLS`: checks that all three mandatory tools have been called before accepting the prediction.

### ForgerAgent

**File:** `agentContainer/agentArchitecture/agent_forger/forger_core.py`

Similar to the PredictiveAgent, but focused on generating and deploying artifacts. It keeps **two** persistent SSE connections: one to the backend (for `get_artifact`/`save_artifact`) and one to mcp-forgery (for `deploy_artifact`).

The intended workflow is: check the knowledge base → generate or retrieve the artifact → deploy it into the honeypot → produce the final JSON as output.

The system instantiates `NUM_PREDICTION` copies of it in a pool, one per prediction, so deployments happen in parallel.

### HoneypotListener (Orchestrator)

**File:** `agentContainer/agentArchitecture/honeypot_listener.py`

It coordinates PredictiveAgent and ForgerAgent through FastAPI's `lifespan` mechanism. At startup it opens all the SSE connections; at shutdown it closes them in an orderly way.

### AttackerAgent

**Files:** `attackerContainer/attacker_core.py`, `attackerContainer/attacker_policies.py`, `attackerContainer/ssh_shell.py`

An autonomous agent that drives a real SSH session against the honeypot as an adaptive generic pentester would (reconnaissance → privilege escalation → sensitive-data discovery). It uses no MCP tools: only a plain multi-turn chat through `AgentConnector` and a persistent SSH channel. Like the other agents, it is an **async context manager**, since it owns a persistent resource (the SSH channel) that must be opened once and closed reliably at the end of the session.

- **`SSHShellSession`** (`ssh_shell.py`): wraps a persistent interactive `paramiko` channel (`invoke_shell()`), required because the fakeshell keeps `cwd` as in-process state for the whole login. The end of each command's output is detected by matching the **next prompt** via a regex on the exact ANSI format emitted by the fakeshell (a sentinel `echo` would silently break on `cd`, which the fakeshell handles before spawning bash). Connection is retried at startup, and if a command hangs (e.g. a pager) a `Ctrl-C` is sent to recover.
- **Turn loop** (`attacker_core.py`): at each turn the LLM replies with exactly one line — either a shell command or `STOP: <reason>`. The shell output is fed back wrapped in `<shell_output>` tags and truncated to 4000 characters.

Termination guardrails:
- `MAX_TURNS`: hard limit on the number of commands per session.
- `STOP: <reason>`: explicit signal from the model when it judges the objective met.
- `LOCAL_RETRY_LIMIT = 3`: empty/malformed replies are retried without consuming the turn budget; after three consecutive failures the session ends.
- The session also ends if the target closes the connection (`exit`/`logout`).

---

## Project Structure

```
agenticPredictiveDeception/
│
├── docker-compose.yml              # Orchestration of the containers
├── .env                            # API key and LLM configuration (→ Docker secret)
├── .gitignore
├── .dockerignore                   # Excludes .git/.env/cache from the root build context
│
├── honeypotContainer/
│   ├── Dockerfile                  # OpenSSH + fakeshell as login shell
│   └── fakeshell.py                # Simulated SSH shell + AI notification
│
├── agentContainer/
│   ├── Dockerfile
│   ├── requirements.txt            # fastapi, uvicorn, google-genai, openai, fastmcp
│   └── agentArchitecture/
│       ├── honeypot_listener.py    # FastAPI app + orchestrator (HoneypotListener)
│       ├── agent_connector.py      # Multi-provider LLM abstraction
│       ├── adapter_connector.py    # Unified wrappers (Google/OpenAI)
│       ├── agent_predictive/
│       │   ├── predictive_core.py  # PredictiveAgent (RAG + prediction)
│       │   ├── predictive_policies.py  # PredictiveAgent system prompt
│       │   └── __init__.py
│       └── agent_forger/
│           ├── forger_core.py      # ForgerAgent (artifact generation + deploy)
│           ├── forger_policies.py  # ForgerAgent system prompt
│           └── __init__.py
│
├── backendContainer/
│   ├── Dockerfile                  # Python + ChromaDB + SentenceTransformers
│   ├── requirements.txt
│   └── mcp_server.py               # MCP server: RAG, sessions, artifacts
│
├── mcpForgeryContainer/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── mcp_server.py               # MCP server: artifact deployment via Docker API
│
└── attackerContainer/
    ├── Dockerfile                  # Build context = repo root (reuses agent_connector.py)
    ├── requirements.txt            # paramiko, google-genai, openai
    ├── main.py                     # Entrypoint: reads env vars and runs one session
    ├── attacker_core.py            # AttackerAgent (turn loop + transcript)
    ├── attacker_policies.py        # AttackerAgent system prompt
    └── ssh_shell.py                # Persistent SSH channel with prompt detection
```

---

## Configuration and Startup

### Prerequisites

- Docker and Docker Compose installed
- A ChromaDB database pre-populated with historical attacks (collection `honeypot_attacks`)
- An API key for an LLM provider (OpenRouter, OpenAI or Google)


### Volume paths (docker-compose.yml)

In `docker-compose.yml`, update the bind mount paths of the `backend` container with the correct local paths:

```yaml
- source: /local/path/chroma_storage   # ChromaDB vector store
  target: /app/data/vector_db
- source: /local/path/sessions         # Per-attacker session logs
  target: /app/data/sessions
- source: /local/path/artifacts        # Generated artifacts (JSONL)
  target: /app/data/artifacts
```

You can also change other parameters (of the `agentic-system` container) such as:
```yaml
environment:
    - PROVIDER=<cloud/local>                                       
    - MODEL_NAME=<model>                                          
    - NUM_PREDICTION=<number of predictions>                         
```

The same `PROVIDER` / `MODEL_NAME` variables, plus `MAX_TURNS`, can be changed in the `attacker` service.

### `.env` file

Create or edit the `.env` file in the project root:

```env
LLM_API_KEY=<your_api_key>
LLM_SDK=openrouter       # or: google, openai
```

### Startup

```bash
git clone https://github.com/melomatte/agenticPredictiveDeception.git
cd agenticPredictiveDeception

docker-compose up --build -d
```

The honeypot will be reachable at `ssh honeypot@localhost -p 2222` (password: `password123`).
It is useful to follow the logs of the other containers with `docker compose logs -f <container-name>`.

### Running the Attacker Agent

The attacker is not started by `docker-compose up`: it belongs to the `attacker` profile and must be launched explicitly, once the rest of the stack is up. Each run performs a single attack session and then exits:

```bash
docker compose --profile attacker run --rm --build attacker
```

The session transcript is written to `attackerContainer/transcripts/attacker_session_<timestamp>.jsonl`. Meanwhile, `docker compose logs -f agentic-system` shows the predictions and artifacts generated in response to the attacker's commands.

---

## Key Design Points

**Proactivity vs. reactivity.** Traditional honeypot systems passively record what an attacker does. This system anticipates the next step and modifies the environment before it happens.

**Forgery parallelism.** `NUM_PREDICTION` ForgerAgents are instantiated in a pool (one per prediction), each with its own SSE connections. The `k` artifacts are generated and deployed in parallel with `asyncio.wait()`.

**Injection without modifying the honeypot.** MCP Forgery leverages the Docker socket (`docker cp` via API) to inject files into the honeypot container from the outside, without the fakeshell having to handle writes or be modified.

**Anti-prompt injection.** Data coming from the attacker (commands, IP, etc.) is always isolated in `<untrusted_data>` tags in the message sent to the LLM, with an explicit instruction to treat it as raw data and not as instructions. Symmetrically, the AttackerAgent receives the shell output wrapped in `<shell_output>` tags, so that planted artifacts cannot hijack it.

**Anti-hallucination guardrail.** The agentic loop checks after execution that all mandatory tools have actually been called. If even one is missing, the prediction is discarded and the forgery phase is not started.

**Multi-provider abstraction.** `AgentConnector` and the wrappers in `adapter_connector.py` make it possible to switch from Google Gemini to OpenAI to OpenRouter (or to a local model via LM Studio) by changing two lines in `.env`, without touching the agents' logic. The AttackerAgent reuses the same abstraction.

**Repeatable end-to-end testing.** The AttackerAgent replaces manual SSH testing with an autonomous, adaptive attacker that produces structured transcripts, making it possible to measure whether planted artifacts are actually found and used.

---

## Research Context

The project explores three areas at the intersection of modern cybersecurity:

**Adaptive cyber deception**: unlike static honeypots (fixed password files, immutable configurations), this system generates contextually appropriate deceptive content for each attacker, based on their specific behavior.

**Agentic AI applied to security**: the agents do not run a simple prompt/response but operate in autonomous loops with tool calling, conversation state management and multi-agent coordination.

**Predictive threat modeling with RAG**: combining session memory (last N commands) with vector retrieval (similar historical attacks) makes it possible to contextualize the prediction both in the present (this session) and in the past (analogous attacks).

---

## Author

**melomatte** — [GitHub Profile](https://github.com/melomatte).
