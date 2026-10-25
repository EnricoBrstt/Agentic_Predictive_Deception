import os
import docker
import tarfile
import io
import time
from fastmcp import FastMCP

mcp = FastMCP("Forgery")
print("[MCP FORGERY] Starting Forgery MCP server...")

# Docker client through the mounted /var/run/docker.sock
docker_client = docker.from_env()

HONEYPOT_CONTAINER_NAME = "agenticpredictivedeception-honeypot-1"

@mcp.tool()
def deploy_artifact(intended_path: str, content: str) -> str:
    """Write a text file into the honeypot container through the Docker API."""
    print(f"\n[MCP FORGERY] Deploying artifact to {intended_path}")

    try:
        honeypot = docker_client.containers.get(HONEYPOT_CONTAINER_NAME)

        file_name = os.path.basename(intended_path)
        dir_name = os.path.dirname(intended_path)

        # In-memory tar holding the single file (mtime = now)
        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode='w') as tar:
            tarinfo = tarfile.TarInfo(name=file_name)
            tarinfo.size = len(content.encode('utf-8'))
            tarinfo.mtime = int(time.time())

            tar.addfile(tarinfo, io.BytesIO(content.encode('utf-8')))

        tar_stream.seek(0)

        # Extract it into the target directory (API equivalent of 'docker cp')
        honeypot.put_archive(path=dir_name, data=tar_stream)

        print(f"[MCP FORGERY] Artifact deployed to {intended_path}")
        return (
            "SUCCESS: The file has been physically created in the honeypot. "
            "CRITICAL INSTRUCTION: DO NOT call any more tools. "
        )

    except docker.errors.NotFound:
        print(f"[MCP FORGERY] ERROR: honeypot container '{HONEYPOT_CONTAINER_NAME}' not found.")
        return f"ERROR: Failed to deploy artifact. Reason: container didn't find"
    except Exception as e:
        print(f"[MCP FORGERY] ERROR deploying artifact: {e}")
        return f"ERROR: Failed to deploy artifact. Reason: {str(e)}"

if __name__ == "__main__":
    print("[MCP FORGERY] MCP server listening on port 8000 (SSE).")
    mcp.run(transport="sse",host="0.0.0.0", port=8000)
