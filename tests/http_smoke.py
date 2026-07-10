"""Connect to a RUNNING LeafBridge server over real Streamable HTTP and confirm
the MCP handshake + tools/list work (this is exactly what Claude/ChatGPT do)."""

from __future__ import annotations

import asyncio
import sys

from fastmcp import Client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/mcp/"


async def run() -> int:
    last_err = None
    for attempt in range(20):
        try:
            async with Client(URL) as client:
                tools = sorted(t.name for t in await client.list_tools())
                print(f"Connected over HTTP. {len(tools)} tools: {tools}")
                # Calling a tool without projects.json should fail *gracefully*
                # with a clear 'not configured' message, not a crash.
                try:
                    await client.call_tool("list_projects", {})
                    print("list_projects returned (server is configured).")
                except Exception as e:  # noqa: BLE001
                    msg = str(e)
                    ok = "not configured" in msg or "projects.json" in msg
                    print(f"list_projects error is graceful: {ok} :: {msg[:120]}")
                return 0 if len(tools) == 11 else 2
        except Exception as e:  # noqa: BLE001 - server may not be up yet
            last_err = e
            await asyncio.sleep(0.4)
    print(f"Could not connect to {URL}: {last_err}")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
