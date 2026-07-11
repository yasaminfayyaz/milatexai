"""Verify AzureTableStore against the REAL Azure Table Storage account.

Uses AZURE_STORAGE_CONNECTION_STRING from .env, runs full CRUD under a throwaway
table prefix, then deletes those test tables. Skips if no connection string.

Run:  .venv\\Scripts\\python.exe tests\\it_azure_store.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(_ROOT / ".env")

_failures: list[str] = []


def check(cond, label):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        _failures.append(label)


async def run():
    from leafbridge.azure_store import AzureTableStore
    from leafbridge.store import Project, User

    cs = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if not cs:
        print("  [skip] no AZURE_STORAGE_CONNECTION_STRING in .env")
        return

    store = AzureTableStore(cs, prefix="ittest")
    try:
        # users
        check(await store.get_user("u1") is None, "missing user -> None")
        await store.upsert_user(User("u1", "a@b.com", plan="pro", is_admin=True))
        u = await store.get_user("u1")
        check(u is not None and u.plan == "pro" and u.is_admin, "user round-trips (plan+admin)")

        # projects + isolation
        await store.put_project(Project("u1", "a" * 24, "thesis", "ENC1", git_url="file:///x"))
        await store.put_project(Project("u2", "b" * 24, "other", "ENC2"))
        mine = await store.list_projects("u1")
        check(len(mine) == 1 and mine[0].name == "thesis", "list_projects is per-user")
        check(mine[0].token_encrypted == "ENC1" and mine[0].git_url == "file:///x",
              "project fields (encrypted token, git_url) round-trip")
        check(await store.delete_project("u1", "a" * 24) is True, "delete existing -> True")
        check(await store.delete_project("u1", "a" * 24) is False, "delete missing -> False")
        check(len(await store.list_projects("u2")) == 1, "other user's project untouched")

        # usage
        check(await store.get_usage("u1", "2026-07") == 0, "usage starts at 0")
        check(await store.increment_usage("u1", "2026-07") == 1, "increment -> 1")
        check(await store.increment_usage("u1", "2026-07", by=4) == 5, "increment by 4 -> 5")
        check(await store.get_usage("u1", "2026-07") == 5, "usage persisted")
        check(await store.get_usage("u1", "2026-08") == 0, "different month independent")
    finally:
        # clean up the throwaway tables
        for base in ("users", "projects", "usage"):
            try:
                await store._svc.delete_table(f"ittest{base}")
            except Exception:
                pass
        await store.close()


def main():
    asyncio.run(run())
    print()
    if _failures:
        print(f"AZURE STORE TEST FAILED: {_failures}")
        return 1
    print("AZURE STORE TEST PASSED — persistence works against real Azure Table Storage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
