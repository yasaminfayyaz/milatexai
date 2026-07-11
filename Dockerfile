# MiLatexAI hosted server — Streamable HTTP MCP at /mcp/
FROM python:3.12-slim

# git is required at runtime (the git worker shells out to it).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY leafbridge/ ./leafbridge/
COPY pyproject.toml README.md ./

# Disposable clone cache on the container's writable layer.
ENV LEAFBRIDGE_DATA_DIR=/tmp/mila-cache \
    PYTHONUNBUFFERED=1

EXPOSE 8000
CMD ["uvicorn", "leafbridge.asgi:app", "--host", "0.0.0.0", "--port", "8000"]
