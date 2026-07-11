"""End-to-end integration test for the LeafBridge engine.

Stands up a local *bare* git repo to stand in for the Overleaf Git bridge, seeds
it with a sample LaTeX project, then drives the real MCP tools through an
in-memory FastMCP client: clone -> list -> read -> sections -> edit -> push, and
verifies the commit actually landed on the "remote". No Overleaf account needed.

Run:  .venv\\Scripts\\python.exe tests\\it_gitflow.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Make the repo root importable and point LeafBridge at a throwaway config +
# cache BEFORE importing the server.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_WORK = Path(tempfile.mkdtemp(prefix="leafbridge_it_"))
_REMOTE = _WORK / "remote.git"
_SEED = _WORK / "seed"
_CACHE = _WORK / "cache"
_VERIFY = _WORK / "verify"
_CONFIG = _WORK / "projects.json"
FAKE_ID = "0123456789abcdef01234567"

os.environ["LEAFBRIDGE_CONFIG"] = str(_CONFIG)
os.environ["LEAFBRIDGE_DATA_DIR"] = str(_CACHE)

from fastmcp import Client  # noqa: E402
from leafbridge.server import mcp  # noqa: E402

MAIN_TEX = r"""\documentclass{article}
\begin{document}
\section{Introduction}
Intro text here.
\section{Methods}
We did science.
\end{document}
"""

# Every byte value, incl. 0x0d (CR), 0x0a (LF), 0x00 — a binary upload must
# survive the git round-trip byte-for-byte.
BLOB = bytes(range(256)) * 4

_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    if not cond:
        _failures.append(label)


def git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr or proc.stdout}")
    return proc.stdout


def setup_remote() -> None:
    _REMOTE.mkdir(parents=True)
    git(["init", "--bare", "-b", "main", "."], cwd=_REMOTE)
    _SEED.mkdir(parents=True)
    git(["init", "-b", "main", "."], cwd=_SEED)
    (_SEED / "main.tex").write_text(MAIN_TEX, encoding="utf-8")
    (_SEED / "refs.bib").write_text("@article{x, title={X}}\n", encoding="utf-8")
    git(["add", "-A"], cwd=_SEED)
    git(
        ["-c", "user.name=Seed", "-c", "user.email=seed@test", "commit", "-m", "init"],
        cwd=_SEED,
    )
    git(["remote", "add", "origin", _REMOTE.as_uri()], cwd=_SEED)
    git(["push", "-u", "origin", "main"], cwd=_SEED)

    _CONFIG.write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "name": "thesis",
                        "url": f"https://www.overleaf.com/project/{FAKE_ID}",
                        "git_url": _REMOTE.as_uri(),
                        "token": "dummy-token-not-used-for-file-remote",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def text_of(result) -> str:
    if isinstance(getattr(result, "data", None), str):
        return result.data
    blocks = getattr(result, "content", None) or []
    return "".join(getattr(b, "text", "") for b in blocks)


async def run() -> None:
    setup_remote()
    async with Client(mcp) as client:
        tools = {t.name for t in await client.list_tools()}
        expected = {
            "list_projects", "list_files", "read_file", "get_sections",
            "read_section", "edit_file", "write_file", "delete_file",
            "upload_file", "get_history", "search", "fetch",
        }
        check(expected <= tools, f"all {len(expected)} tools registered")

        r = text_of(await client.call_tool("list_projects", {}))
        check("thesis" in r, "list_projects shows the project")

        r = text_of(await client.call_tool("list_files", {}))
        check("main.tex" in r and "refs.bib" in r, "list_files lists source files")

        r = text_of(await client.call_tool("read_file", {"path": "main.tex"}))
        check("\\section{Introduction}" in r, "read_file returns content")
        check(r.splitlines()[0].lstrip().startswith("1\t"), "read_file numbers lines")

        r = text_of(await client.call_tool("get_sections", {"path": "main.tex"}))
        check("Introduction" in r and "Methods" in r, "get_sections finds sections")

        r = text_of(
            await client.call_tool(
                "read_section", {"path": "main.tex", "title": "Introduction"}
            )
        )
        check("Intro text here." in r, "read_section returns the section body")

        # The metered operation: edit + commit + push.
        r = text_of(
            await client.call_tool(
                "edit_file",
                {
                    "path": "main.tex",
                    "old_string": "Intro text here.",
                    "new_string": "Rewritten intro paragraph.",
                },
            )
        )
        check("Committed" in r and "pushed" in r, "edit_file commits and pushes")

        r = text_of(await client.call_tool("get_history", {"limit": 5}))
        check("Edit main.tex" in r, "get_history shows the new commit")

        r = text_of(await client.call_tool("read_file", {"path": "main.tex"}))
        check("Rewritten intro paragraph." in r, "edit is reflected on re-read")

        # search + fetch (ChatGPT contract)
        s = await client.call_tool("search", {"query": "Rewritten"})
        results = (s.data or {}).get("results", [])
        check(bool(results), "search returns a hit for edited text")
        if results:
            f = await client.call_tool("fetch", {"id": results[0]["id"]})
            check(
                "Rewritten intro paragraph." in (f.data or {}).get("text", ""),
                "fetch returns full document text",
            )

        # write_file then delete_file
        r = text_of(
            await client.call_tool(
                "write_file", {"path": "notes/todo.tex", "content": "% todo\n"}
            )
        )
        check("Committed" in r, "write_file creates + pushes a new file")
        r = text_of(await client.call_tool("delete_file", {"path": "notes/todo.tex"}))
        check("Committed" in r, "delete_file removes + pushes")

        # Binary upload (image-style) must round-trip byte-exact.
        import base64 as _b64

        r = text_of(
            await client.call_tool(
                "upload_file",
                {"path": "figures/blob.bin", "content_base64": _b64.b64encode(BLOB).decode()},
            )
        )
        check("Committed" in r, "upload_file pushes a binary file")

    # Independent verification: the push really reached the "remote".
    git(["clone", _REMOTE.as_uri(), str(_VERIFY)], cwd=_WORK)
    landed = (_VERIFY / "main.tex").read_bytes().decode("utf-8")
    check("Rewritten intro paragraph." in landed, "commit landed on the remote")
    check(not (_VERIFY / "notes" / "todo.tex").exists(), "deleted file gone on remote")
    check(
        (_VERIFY / "figures" / "blob.bin").read_bytes() == BLOB,
        "binary file is byte-identical on the remote",
    )
    # Line-ending integrity: the STORED file (blob) is what Overleaf compiles and
    # must have NO stray CRs. (A plain clone's working copy may gain CRLF from the
    # system autocrlf=true, so we check the blob, not the checkout.)
    stored = subprocess.run(
        ["git", "-C", str(_VERIFY), "show", "HEAD:main.tex"], capture_output=True
    ).stdout
    check(b"\r" not in stored, "stored file has no stray CRs (no line-ending doubling)")


def main() -> int:
    try:
        asyncio.run(run())
    finally:
        # Best-effort cleanup of the throwaway workspace.
        import shutil
        import stat

        def _rm(func, p, _):
            try:
                os.chmod(p, stat.S_IWRITE)
                func(p)
            except Exception:
                pass

        shutil.rmtree(_WORK, onerror=_rm)

    print()
    if _failures:
        print(f"INTEGRATION TEST FAILED: {len(_failures)} check(s) failed:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("INTEGRATION TEST PASSED — the full Overleaf-style git flow works.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
