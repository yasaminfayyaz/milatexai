"""Shared state for several running copies, against the REAL Azure storage account.

Uses AZURE_STORAGE_CONNECTION_STRING from .env, works only under a throwaway
"itmc" prefix (its own tables and blob container), and deletes them afterwards.
Checks, with two independent store objects standing in for two server copies:

  - 2 x 40 simultaneous commit counts lose nothing (optimistic concurrency),
  - the last-pushed-commit record round-trips between copies,
  - a download written by one copy is readable by the other, and a missing one is None.

Run:  .venv\\Scripts\\python.exe tests\\it_shared_state_azure.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_ROOT / ".env")
PREFIX = f"itmc{int(time.time()) % 1000000}"   # unique per run: Azure takes a while to finish deleting tables
failures: list[str] = []


def check(cond, label):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


async def run():
    from azure.data.tables.aio import TableServiceClient
    from azure.storage.blob.aio import BlobServiceClient

    from leafbridge.azure_store import AzureTableStore

    cs = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if not cs:
        print("  [skip] no AZURE_STORAGE_CONNECTION_STRING")
        return
    copy1, copy2 = AzureTableStore(cs, prefix=PREFIX), AzureTableStore(cs, prefix=PREFIX)
    try:
        month = time.strftime("%Y-%m")
        started = time.monotonic()
        await asyncio.gather(*([copy1.increment_usage("u1", month) for _ in range(40)]
                               + [copy2.increment_usage("u1", month) for _ in range(40)]))
        total = await copy1.get_usage("u1", month)
        check(total == 80, f"80 simultaneous counts from two copies all landed (got {total}, {time.monotonic() - started:.1f}s)")

        check(await copy2.get_head("abc123") is None, "no record yet for a new project")
        await copy1.put_head("abc123", "f" * 40)
        check(await copy2.get_head("abc123") == "f" * 40, "a commit recorded by copy 1 is seen by copy 2")
        await copy2.put_head("abc123", "e" * 40)
        check(await copy1.get_head("abc123") == "e" * 40, "a newer record replaces the older one")
        await copy1.put_head("github-owner/repo#x?y", "d" * 40)
        check(await copy2.get_head("github-owner/repo#x?y") == "d" * 40, "project ids with characters keys forbid still work")

        blob = os.urandom(300_000)
        await copy1.put_download("bundle.zip", blob)
        check(await copy2.get_download("bundle.zip") == blob, "a 300 KB bundle written by copy 1 is read intact by copy 2")
        check(await copy2.get_download("missing.zip") is None, "a missing bundle reads as None")
    finally:
        await copy1.close()
        await copy2.close()
        svc = TableServiceClient.from_connection_string(cs)
        async with svc:
            for base in ("users", "projects", "usage", "heads"):
                try:
                    await svc.delete_table(f"{PREFIX}{base}")
                except Exception:  # noqa: BLE001
                    pass
        bsvc = BlobServiceClient.from_connection_string(cs)
        async with bsvc:
            try:
                await bsvc.delete_container(f"{PREFIX}downloads")
            except Exception:  # noqa: BLE001
                pass
        print("  cleaned up the throwaway tables and container")


asyncio.run(run())
print("ALL PASS" if not failures else f"{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
