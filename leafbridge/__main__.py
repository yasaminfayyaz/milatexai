"""``python -m leafbridge`` — start the local LeafBridge MCP server.

Serves the Streamable HTTP transport that Claude and ChatGPT connect to. The
connector URL to paste into Claude / ChatGPT is printed on startup.
"""

from __future__ import annotations

import os

from .server import mcp


def main() -> None:
    host = os.environ.get("LEAFBRIDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("LEAFBRIDGE_PORT", "8000"))

    url = f"http://{host}:{port}/mcp/"
    print("=" * 60)
    print("  LeafBridge — Overleaf AI Connector (local mode)")
    print("=" * 60)
    print(f"  MCP endpoint:  {url}")
    print("  Add this URL as a custom connector in Claude, or as a")
    print("  developer-mode app in ChatGPT.")
    print("  Press Ctrl+C to stop.")
    print("=" * 60)

    mcp.run(transport="http", host=host, port=port)


if __name__ == "__main__":
    main()
