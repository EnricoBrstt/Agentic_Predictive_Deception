FROM python:3.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && \
    apt-get install -y build-essential python3-dev && \
    rm -rf /var/lib/apt/lists/*

# Build context = root del repo (vedi docker-compose.yml), cosi' possiamo riusare l'astrazione
# multi-provider LLM gia' scritta per agentic-system senza duplicarla manualmente nel repo.
# Source of truth: agentContainer/agentArchitecture/agent_connector.py e adapter_connector.py.
COPY agentContainer/agentArchitecture/agent_connector.py agentContainer/agentArchitecture/adapter_connector.py ./

COPY attackerContainer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY attackerContainer/*.py ./

RUN mkdir -p /app/transcripts

CMD ["python", "main.py"]
