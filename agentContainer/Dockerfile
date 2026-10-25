FROM python:3.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Build tools for packages with native extensions
RUN apt-get update && \
    apt-get install -y build-essential python3-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Volume mount points
RUN mkdir -p /app/data/vector_db /app/data/sessions

# Agent code (agentArchitecture/)
COPY . .

EXPOSE 8000

# Run from the package directory so its modules import directly
WORKDIR /app/agentArchitecture

CMD ["uvicorn", "honeypot_listener:app", "--host", "0.0.0.0", "--port", "8000"]