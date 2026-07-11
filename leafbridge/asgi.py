"""Production ASGI entry point for the hosted MiLatexAI server.

    uvicorn leafbridge.asgi:app --host 0.0.0.0 --port 8000

Selects the persistent Azure Table Storage backend when a connection string is
configured, else falls back to the in-memory store (dev only). Requires
WORKOS_AUTHKIT_DOMAIN and LEAFBRIDGE_ENC_KEY in the environment.
"""

from __future__ import annotations

import os

from .hosted import create_hosted_server
from .store import InMemoryStore, Store


def store_from_env() -> Store:
    if os.environ.get("AZURE_STORAGE_CONNECTION_STRING"):
        from .azure_store import AzureTableStore

        return AzureTableStore.from_env()
    return InMemoryStore()


def build_app():
    mcp = create_hosted_server(
        store=store_from_env(),
        auth=True,
        base_url=os.environ.get("BASE_URL", "https://milatexai.com"),
    )
    return mcp.http_app()


app = build_app()
