"""Production ASGI entry point for the hosted MiLatexAI server.

    uvicorn leafbridge.asgi:app --host 0.0.0.0 --port 8000

Selects the persistent Azure Table Storage backend when a connection string is
configured, else falls back to the in-memory store (dev only). Requires
WORKOS_AUTHKIT_DOMAIN and LEAFBRIDGE_ENC_KEY in the environment.
"""

from __future__ import annotations

import os

from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

from .hosted import create_hosted_server
from .store import InMemoryStore, Store

# The OAuth authorization flow and the /.well-known/* discovery documents are
# fetched from the assistant's *browser* origin (notably chatgpt.com); the MCP
# tool traffic itself is server-to-server and unaffected by CORS. Allow the
# OpenAI/ChatGPT origins so the connect flow works there as it does for Claude.
_CORS = Middleware(
    CORSMiddleware,
    allow_origins=[
        "https://chatgpt.com",
        "https://chat.openai.com",
        "https://platform.openai.com",
    ],
    allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
    allow_headers=["Content-Type", "Authorization", "MCP-Protocol-Version", "Mcp-Session-Id"],
    max_age=3600,
)


class _SecurityHeaders:
    """Emit ``Referrer-Policy: no-referrer`` on every response, so one-time connect
    codes and OAuth ``state``/session values that ride in URLs never leak through
    the Referer header when a page links out (e.g. to overleaf.com)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def _send(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append(
                    (b"referrer-policy", b"no-referrer")
                )
            await send(message)

        await self.app(scope, receive, _send)


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
    # stateless_http=True: ChatGPT's Streamable HTTP client issues DELETE /mcp
    # after a tool call, which makes a *stateful* FastMCP session 404 with
    # "Session terminated" (the single most common FastMCP<->ChatGPT failure).
    # This server holds no per-session memory — all state is per-user in the
    # store — so stateless mode is safe here and keeps Claude working too.
    return mcp.http_app(
        stateless_http=True, middleware=[_CORS, Middleware(_SecurityHeaders)]
    )


app = build_app()
