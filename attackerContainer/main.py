"""Entrypoint dell'attacker container: legge la configurazione dall'ambiente ed esegue una
singola sessione di attacco contro l'honeypot."""

import asyncio
import os
import time

from attacker_core import AttackerAgent

HONEYPOT_HOST = os.getenv("HONEYPOT_HOST", "honeypot")
HONEYPOT_PORT = os.getenv("HONEYPOT_PORT", "2222")
HONEYPOT_USER = os.getenv("HONEYPOT_USER", "honeypot")
HONEYPOT_PASSWORD = os.getenv("HONEYPOT_PASSWORD", "password123")
PROVIDER = os.getenv("PROVIDER")
MODEL_NAME = os.getenv("MODEL_NAME")
MAX_TURNS = os.getenv("MAX_TURNS", "25")
TRANSCRIPT_DIR = os.getenv("TRANSCRIPT_DIR", "/app/transcripts")


async def main():
    os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
    transcript_path = os.path.join(
        TRANSCRIPT_DIR, f"attacker_session_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    )
    print(f"[ATTACKER MAIN] Transcript: {transcript_path}")

    async with AttackerAgent(
        host=HONEYPOT_HOST,
        port=HONEYPOT_PORT,
        username=HONEYPOT_USER,
        password=HONEYPOT_PASSWORD,
        model_name=MODEL_NAME,
        provider=PROVIDER,
        max_turns=MAX_TURNS,
        transcript_path=transcript_path,
    ) as agent:
        await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
