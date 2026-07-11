"""``python -m leafbridge`` — start the LeafBridge MCP server.

Two transports:

* ``--stdio``  — the server is launched by the client (Claude Code / Claude
  Desktop) and talks over stdin/stdout. Easiest for local use: no port, no
  separate process to keep running.
* (default) ``--http`` — Streamable HTTP at ``http://<host>:<port>/mcp/``, for
  remote connectors (a tunnel today, the hosted service in Phase 2) and ChatGPT.
"""

from __future__ import annotations

import argparse
import os
import sys

from .server import mcp


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="leafbridge",
        description="LeafBridge — edit your Overleaf projects from Claude/ChatGPT.",
    )
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="Run over stdio (for Claude Code / Claude Desktop launching it locally).",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Run over Streamable HTTP (default; for remote connectors / ChatGPT).",
    )
    parser.add_argument("--host", default=os.environ.get("LEAFBRIDGE_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("LEAFBRIDGE_PORT", "8000"))
    )
    args = parser.parse_args()

    if args.stdio and not args.http:
        # IMPORTANT: in stdio mode stdout is the JSON-RPC channel — never print
        # to stdout here. Diagnostics go to stderr only.
        print("LeafBridge starting over stdio …", file=sys.stderr)
        mcp.run(show_banner=False)  # default transport is stdio
        return

    url = f"http://{args.host}:{args.port}/mcp/"
    print("=" * 60)
    print("  LeafBridge — Overleaf AI Connector (local HTTP mode)")
    print("=" * 60)
    print(f"  MCP endpoint:  {url}")
    print("  • Claude Code:  claude mcp add --transport http leafbridge " + url)
    print("  • Remote (claude.ai/ChatGPT): expose this via a tunnel (Phase 2).")
    print("  Press Ctrl+C to stop.")
    print("=" * 60)
    mcp.run(transport="http", host=args.host, port=args.port, show_banner=False)


if __name__ == "__main__":
    main()
