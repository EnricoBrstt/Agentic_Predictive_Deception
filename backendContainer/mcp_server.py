import os
import json
from typing import List
from collections import deque
from fastmcp import FastMCP
import chromadb
from chromadb.utils import embedding_functions

# Paths inside the container (bind-mounted from the host, see docker-compose.yml)
DB_PATH = "/app/data/vector_db"
LOG_PATH = "/app/data/sessions"
ARTIFACTS_PATH = "/app/data/artifacts/artifacts.jsonl"

mcp = FastMCP("Agent-Backend")
print("[BACKEND] Starting Agent-Backend MCP server...")

# --- ChromaDB ---
print(f"[BACKEND] Opening ChromaDB at {DB_PATH}...")
emb_fn = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
db_client = chromadb.PersistentClient(path=DB_PATH)

try:
    collection = db_client.get_collection(name="honeypot_attacks", embedding_function=emb_fn)
    print("[BACKEND] Collection 'honeypot_attacks' loaded.")
except Exception as e:
    print("[BACKEND] ERROR: collection 'honeypot_attacks' not found. Was the indexing script run?")
    raise e

# --- PredictiveAgent tools ---

@mcp.tool()
def retrieve(current_context_list: List[str], k: int) -> str:
    """Query the vector DB and return similar past attacks."""
    print(f"\n[BACKEND][RAG] Retrieve request, context of {len(current_context_list)} commands.")

    if not current_context_list:
        print("[BACKEND][RAG] Empty context, search skipped.")
        return ""

    query_text = " || ".join(current_context_list)
    print(f"[BACKEND][RAG] Query: '{query_text}'")

    results = collection.query(
        query_texts=[query_text],
        n_results=k
    )

    if not results['ids'] or len(results['ids'][0]) == 0:
        print("[BACKEND][RAG] No similar attacks found.")
        return ""

    ids = results['ids'][0]
    docs = results['documents'][0]
    metas = results['metadatas'][0]

    formatted_examples = ""

    for i in range(len(ids)):
        hist_ctx = docs[i].replace(" || ", "\n")
        hist_next = metas[i]['next_command']

        formatted_examples += (
            f"--- SIMILAR PAST ATTACK (Example {i+1}) ---\n"
            f"Context:\n{hist_ctx}\n"
            f"Attacker Next Move:\n{hist_next}\n\n"
        )

    print(f"[BACKEND][RAG] Found {len(ids)} similar attacks.")
    return formatted_examples

@mcp.tool()
def log_session_event(session_id: str, event_data: dict) -> bool:
    """Append a command event to the session log file."""
    print(f"\n[BACKEND][LOG] New event for session {session_id}")

    os.makedirs(LOG_PATH, exist_ok=True)
    file_path = os.path.join(LOG_PATH, f"session_{session_id}.jsonl")

    with open(file_path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(event_data) + '\n')

    print(f"[BACKEND][LOG] Written to {file_path}")
    return True

@mcp.tool()
def get_session_history(session_id: str, window_size: int) -> list:
    """Return the last window_size commands of the session."""
    print(f"\n[BACKEND][LOG] Reading last {window_size} commands of session {session_id}")

    file_path = os.path.join(LOG_PATH, f"session_{session_id}.jsonl")

    if not os.path.exists(file_path):
        print("[BACKEND][LOG] No session file yet, empty history.")
        return []

    context_history = []
    with open(file_path, 'r', encoding='utf-8') as f:
        last_lines = deque(f, maxlen=window_size)

        for line in last_lines:
            if line.strip():
                try:
                    parsed_line = json.loads(line)
                    if 'cmd' in parsed_line:
                        context_history.append(parsed_line['cmd'])
                except json.JSONDecodeError:
                    continue

    print(f"[BACKEND][LOG] Read {len(context_history)} commands.")
    return context_history

import json

# --- ForgerAgent tools ---

@mcp.tool()
def save_artifact(predicted_command: str, artifact_data: dict | str) -> bool:
    """Append an artifact to the JSONL cache; accepts a dict or a raw JSON string."""
    print(f"\n[BACKEND][ARTIFACTS] Saving artifact for '{predicted_command}'")

    # The LLM may pass the artifact as a JSON string
    if isinstance(artifact_data, str):
        try:
            artifact_data = json.loads(artifact_data)
        except json.JSONDecodeError as e:
            print(f"[BACKEND][ARTIFACTS] Invalid JSON from the agent: {e}")
            return False

    os.makedirs(os.path.dirname(ARTIFACTS_PATH), exist_ok=True)

    record = {
        "command": predicted_command,
        "artifact": artifact_data
    }

    try:
        with open(ARTIFACTS_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record) + '\n')
        print("[BACKEND][ARTIFACTS] Artifact saved.")
        return True
    except Exception as e:
        print(f"[BACKEND][ARTIFACTS] ERROR saving artifact: {e}")
        return False

@mcp.tool()
def get_artifact(predicted_command: str) -> str:
    """Return the latest cached artifact for the command as a JSON string, or "" if there is none."""
    print(f"\n[BACKEND][ARTIFACTS] Looking up artifact for '{predicted_command}'")

    if not os.path.exists(ARTIFACTS_PATH):
        print("[BACKEND][ARTIFACTS] No artifacts.jsonl yet.")
        return ""

    latest_match = None

    try:
        with open(ARTIFACTS_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    data = json.loads(line)
                    # Keep the last match: the most recent artifact wins
                    if data.get("command") == predicted_command:
                        latest_match = data.get("artifact")

    except json.JSONDecodeError:
        print("[BACKEND][ARTIFACTS] Corrupted line in artifacts.jsonl.")

    if latest_match:
        print(f"[BACKEND][ARTIFACTS] Cache hit for '{predicted_command}'.")
        # Only the 'artifact' object, as produced by the forger
        return json.dumps(latest_match)

    print("[BACKEND][ARTIFACTS] Cache miss.")
    return ""

if __name__ == "__main__":
    print("[BACKEND] MCP server listening on port 8000 (SSE).")
    mcp.run(transport="sse",host="0.0.0.0", port=8000)
