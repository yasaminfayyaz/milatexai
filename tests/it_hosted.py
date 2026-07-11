"""End-to-end test of the HOSTED (multi-tenant, metered) server.

Drives the real hosted tools through an in-memory FastMCP client, with a fake
identity provider (no WorkOS needed) and a local bare repo standing in for
Overleaf. Covers: onboarding, per-user isolation, real edit->commit->push, and
the free-tier commit limit. Also checks the production assembly builds with auth.

Run:  .venv\\Scripts\\python.exe tests\\it_hosted.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_WORK = Path(tempfile.mkdtemp(prefix="mila_hosted_"))
_REMOTE = _WORK / "remote.git"
_SEED = _WORK / "seed"
_CACHE = _WORK / "cache"
os.environ["LEAFBRIDGE_DATA_DIR"] = str(_CACHE)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(_ROOT / ".env")

from fastmcp import Client  # noqa: E402
from leafbridge.hosted import create_hosted_server, _month  # noqa: E402
from leafbridge.service import AccountService  # noqa: E402
from leafbridge.store import InMemoryStore, TokenCipher  # noqa: E402

ID1 = "0123456789abcdef01234567"
ID2 = "89abcdef0123456789abcdef"
USER1, EMAIL1 = "user_alice", "alice@example.com"
USER2, EMAIL2 = "user_bob", "bob@example.com"

_failures: list[str] = []


def check(cond, label):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        _failures.append(label)


def git(args, cwd):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"git {args}: {p.stderr or p.stdout}")
    return p.stdout


def text_of(res):
    if isinstance(getattr(res, "data", None), str):
        return res.data
    return "".join(getattr(b, "text", "") for b in (getattr(res, "content", None) or []))


def setup_remote():
    _REMOTE.mkdir(parents=True)
    git(["init", "--bare", "-b", "main", "."], cwd=_REMOTE)
    _SEED.mkdir(parents=True)
    git(["init", "-b", "main", "."], cwd=_SEED)
    (_SEED / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n\\section{Intro}\nHello.\n\\end{document}\n",
        encoding="utf-8",
    )
    git(["add", "-A"], cwd=_SEED)
    git(["-c", "user.name=S", "-c", "user.email=s@t", "commit", "-m", "init"], cwd=_SEED)
    git(["remote", "add", "origin", _REMOTE.as_uri()], cwd=_SEED)
    git(["push", "-u", "origin", "main"], cwd=_SEED)


async def run():
    setup_remote()
    store = InMemoryStore()
    cipher = TokenCipher(TokenCipher.generate_key())
    svc = AccountService(store, cipher)

    # Alice exists with one project wired to the local "Overleaf" remote.
    await svc.get_or_create_user(USER1, EMAIL1)
    await svc.connect_project(
        USER1, f"https://www.overleaf.com/project/{ID1}", "tok", "thesis",
        git_url=_REMOTE.as_uri(),
    )

    identity = {"id": USER1, "email": EMAIL1}
    mcp = create_hosted_server(
        store=store, cipher=cipher, auth=False, data_dir=_CACHE,
        identity_provider=lambda: (identity["id"], identity["email"]),
    )

    async with Client(mcp) as c:
        tools = {t.name for t in await c.list_tools()}
        check({"connect_project", "list_projects", "edit_file", "list_files"} <= tools,
              f"hosted tools registered ({len(tools)})")

        # --- Alice: her project resolves and edits work ---
        r = text_of(await c.call_tool("list_projects", {}))
        check("thesis" in r, "list_projects shows Alice's project")
        r = text_of(await c.call_tool("list_files", {}))
        check("main.tex" in r, "list_files reads Alice's Overleaf clone")
        r = text_of(await c.call_tool("edit_file",
                    {"path": "main.tex", "old_string": "Hello.", "new_string": "Rewritten by MiLatexAI."}))
        check("Committed" in r and "commit 1 this month" in r, "edit commits, pushes, counts usage")

        # --- token is stored ENCRYPTED ---
        stored = (await store.list_projects(USER1))[0]
        check(stored.token_encrypted != "tok" and "tok" not in stored.token_encrypted,
              "stored token is encrypted")

        # --- free plan: 1 project, so a 2nd distinct project is refused ---
        try:
            await c.call_tool("connect_project",
                              {"overleaf_url": f"https://www.overleaf.com/project/{ID2}", "token": "t2"})
            check(False, "second project should hit the free limit")
        except Exception as e:  # noqa: BLE001
            check("Upgrade to Pro" in str(e), "free plan blocks a 2nd project with upgrade prompt")

        # --- per-user isolation: Bob sees nothing / can't touch Alice's project ---
        identity["id"], identity["email"] = USER2, EMAIL2
        r = text_of(await c.call_tool("list_projects", {}))
        check("No projects connected" in r, "Bob sees none of Alice's projects")
        try:
            await c.call_tool("read_file", {"path": "main.tex"})
            check(False, "Bob should not read Alice's file")
        except Exception as e:  # noqa: BLE001
            check("No Overleaf project is connected" in str(e), "Bob is isolated from Alice's data")

        # --- metering: push Alice to the monthly cap, next edit is blocked ---
        identity["id"], identity["email"] = USER1, EMAIL1
        await store.increment_usage(USER1, _month(), 24)  # 1 (done) + 24 = 25 = cap
        try:
            await c.call_tool("edit_file",
                              {"path": "main.tex", "old_string": "Rewritten by MiLatexAI.", "new_string": "x"})
            check(False, "edit past the cap should be blocked")
        except Exception as e:  # noqa: BLE001
            check("all 25 free commits" in str(e), "free monthly commit cap is enforced")

    # --- the edit really reached the remote ---
    verify = _WORK / "verify"
    git(["clone", _REMOTE.as_uri(), str(verify)], cwd=_WORK)
    check("Rewritten by MiLatexAI." in (verify / "main.tex").read_text(encoding="utf-8"),
          "hosted edit landed on the remote")

    # --- production assembly builds with real WorkOS auth ---
    if os.environ.get("WORKOS_AUTHKIT_DOMAIN"):
        prod = create_hosted_server(auth=True, base_url="https://milatexai.com")
        check(prod is not None, "production server builds with AuthKit auth")
    else:
        print("  [skip] production auth build (no WORKOS_AUTHKIT_DOMAIN in env)")


def main():
    try:
        asyncio.run(run())
    finally:
        import shutil, stat
        def _rm(f, p, _):
            try:
                os.chmod(p, stat.S_IWRITE); f(p)
            except Exception:
                pass
        shutil.rmtree(_WORK, onerror=_rm)
    print()
    if _failures:
        print(f"HOSTED TEST FAILED: {_failures}")
        return 1
    print("HOSTED TEST PASSED — multi-tenant auth, isolation, edits, and metering all work.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
