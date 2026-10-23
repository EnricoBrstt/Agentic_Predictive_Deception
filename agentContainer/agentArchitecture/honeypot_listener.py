"""
HoneypotListener: FastAPI entry point of the agentic system.

fakeshell sends every command typed in the honeypot to POST /new_command, which runs the pipeline:
    1. PredictiveAgent predicts the attacker's next k commands.
    2. One ForgerAgent per prediction creates and deploys a fake artifact, all in parallel.

The agents keep persistent SSE connections to the MCP servers: the FastAPI lifespan opens them at
startup and closes them at shutdown, and the listener is stored in app.state.orch.
"""

from contextlib import asynccontextmanager
import os
import asyncio
from typing import List
from fastapi import FastAPI
from pydantic import BaseModel
from agent_predictive import PredictiveAgent
from agent_forger import ForgerAgent

# Configuration from the container environment (see docker-compose.yml)
PROVIDER = os.getenv("PROVIDER")
MODEL_NAME = os.getenv("MODEL_NAME")
BACKEND_MCP_URL = os.getenv("BACKEND_MCP_URL")
FORGERY_MCP_URL = os.getenv("FORGERY_MCP_URL")
NUM_PREDICTION = os.getenv("NUM_PREDICTION")

# Event sent by fakeshell for each command
class CommandEvent(BaseModel):
    session_id: str
    timestamp: str
    ip: str
    user: str
    cwd: str
    cmd: str

class HoneypotListener:
    def __init__(self):
        self.predictive = PredictiveAgent(mcp_url=BACKEND_MCP_URL, model_name=MODEL_NAME, provider=PROVIDER, k=NUM_PREDICTION)
        self.forgers: List[ForgerAgent] = [ForgerAgent(mcp_url_backend=BACKEND_MCP_URL, mcp_url_forgery=FORGERY_MCP_URL,model_name=MODEL_NAME, provider=PROVIDER, id= i) for i in range(int(NUM_PREDICTION))]

    async def __aenter__(self):
        print("[HONEYPOT LISTENER] Opening agent connections...")
        await self.predictive.__aenter__()
        for forger in self.forgers:
            await forger.__aenter__()
        print("[HONEYPOT LISTENER] Agent connections open.")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        print("[HONEYPOT LISTENER] Closing agent connections...")
        await self.predictive.__aexit__(exc_type, exc_val, exc_tb)
        for forger in self.forgers:
            await forger.__aexit__(exc_type, exc_val, exc_tb)
        print("[HONEYPOT LISTENER] Agent connections closed.")

    async def dispatch(self, event: CommandEvent):
        print(f"\n[HONEYPOT LISTENER] New command: '{event.cmd}'")

        # --- PHASE 1: PREDICTION + RAG ---
        print(f"[HONEYPOT LISTENER] Phase 1: predicting next commands...")
        predicted_cmd = await self.predictive.decide(event)
        print(f"[HONEYPOT LISTENER] Predicted commands: {predicted_cmd}")

        if not predicted_cmd:
            print("[HONEYPOT LISTENER] No predictions, skipping artifact creation.")
            return

        # --- PHASE 2: FORGERS ---
        num_active_forgers = min(len(predicted_cmd), len(self.forgers))
        print(f"\n[HONEYPOT LISTENER] Phase 2: running {num_active_forgers} forger agents in parallel...")
        tasks = [
            forger.decide(cmd, event)
            for cmd, forger in zip(predicted_cmd, self.forgers)
        ]
        asyncio_tasks = [asyncio.create_task(t) for t in tasks]

        # Wait at most 3 minutes; late agents are cancelled below
        done, pending = await asyncio.wait(asyncio_tasks, timeout=180.0)

        result = []

        for task in done:
            try:
                # result() re-raises the exception of a crashed agent
                res = task.result()
                result.append(res)
            except Exception as e:
                print(f"[HONEYPOT LISTENER] WARNING: forger agent failed: {e}")
                result.append([])

        if pending:
            print(f"\n[HONEYPOT LISTENER] WARNING: timeout, cancelling {len(pending)} unfinished agents.")
            for task in pending:
                task.cancel()
        print(f"[HONEYPOT LISTENER] Generated artifacts: {result}")
        print("[HONEYPOT LISTENER] Phase 2 done.")

# --- Lifespan: agent connections are opened at startup and closed at shutdown ---

@asynccontextmanager
async def lifespan(app: FastAPI):

    async with HoneypotListener() as orch:
        print("[HONEYPOT LISTENER] Server ready to receive events.")
        app.state.orch = orch
        yield


app = FastAPI(lifespan=lifespan)

@app.post("/new_command")
async def handle_new_command(event: CommandEvent):
    """Receive a command event from fakeshell and run the pipeline on it."""
    await app.state.orch.dispatch(event)
    return {"status": "success", "message": "Evento processato sequenzialmente"}
