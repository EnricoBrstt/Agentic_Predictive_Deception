FROM python:3.10-slim

# No .pyc files, unbuffered logs
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y \
    build-essential \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements first, to reuse the Docker layer cache
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY mcp_server.py .

# FastMCP SSE server
EXPOSE 8000

CMD ["python", "mcp_server.py"]