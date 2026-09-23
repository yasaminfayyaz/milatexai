"""End-to-end: the whole product, through the real wire.

Starts the hosted MCP server on a real HTTP port (Streamable HTTP, stateless,
the same transport Claude and ChatGPT use), connects a real MCP client to it,
and drives a researcher's full workflow against real git remotes: connect two
projects, read, edit, write, checkpoint, restore, move a figure between
projects, rename, then compile, render, export for arXiv with a real
bibliography, and produce a tracked-changes PDF. Every write is verified by
cloning the remote independently, never by trusting the server's own report.

LaTeX steps need Tectonic and latexdiff. Locally they skip when those are
missing; in CI this suite runs INSIDE the production image with
E2E_REQUIRE_LATEX=1, where a missing engine is a failure, not a skip.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn

from leafbridge import texcompile
from leafbridge.capacity import CapacityGate
from leafbridge.hosted import create_hosted_server
from leafbridge.service import AccountService
from leafbridge.store import InMemoryStore, TokenCipher, User

REQUIRE_LATEX = os.environ.get("E2E_REQUIRE_LATEX") == "1"

HEX_A = "aaaaaaaaaaaaaaaaaaaaaaaa"
HEX_B = "bbbbbbbbbbbbbbbbbbbbbbbb"

MAIN_TEX = (
    "\\documentclass{article}\n"
    "\\begin{document}\n"
    "\\section{Introduction}\n"
    "Quantum networks are fragile \\cite{knuth84}.\n"
    "\\input{sections/method}\n"
    "\\bibliographystyle{plain}\n"
    "\\bibliography{refs}\n"
    "\\end{document}\n"
)
METHOD_TEX = "\\section{Method}\nWe measure things carefully.\n"
REFS_BIB = (
    "@article{knuth84,\n"
    "  author = {Donald E. Knuth},\n"
    "  title = {Literate Programming},\n"
    "  journal = {The Computer Journal},\n"
    "  year = {1984},\n"
    "  volume = {27},\n"
    "  pages = {97--111}\n"
    "}\n"
)
FIGURE_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4  # binary, every byte value


# --- helpers ------------------------------------------------------------------

def _git(args, cwd) -> str:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, p.stderr or p.stdout
    return p.stdout


def _make_remote(root: Path, name: str, files: dict[str, bytes]) -> Path:
    remote, seed = root / f"{name}.git", root / f"seed_{name}"
    remote.mkdir()
    _git(["init", "--bare", "-b", "main", "."], remote)
    seed.mkdir()
    _git(["init", "-b", "main", "."], seed)
    for rel, data in files.items():
        p = seed / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    _git(["add", "-A"], seed)
    _git(["-c", "user.name=Seed", "-c", "user.email=seed@example.com",
          "commit", "-m", "init"], seed)
    _git(["remote", "add", "origin", remote.as_uri()], seed)
    _git(["push", "-u", "origin", "main"], seed)
    return remote


def _on_remote(world, remote: Path, rel: str) -> bytes:
    """Independent check: the exact committed bytes on the remote's main
    branch (what Overleaf/GitHub actually serve), read straight from the bare
    repo so no checkout line-ending conversion can mask a difference."""
    p = subprocess.run(["git", "show", f"main:{rel}"], cwd=str(remote), capture_output=True)
    assert p.returncode == 0, p.stderr.decode(errors="replace")
    return p.stdout


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _call(world, tool: str, args: dict | None = None):
    from fastmcp import Client

    async def go():
        async with Client(world.mcp_url) as c:
            return await c.call_tool(tool, args or {})
    return asyncio.run(go())


def _text(result) -> str:
    return "".join(getattr(b, "text", "") for b in (result.content or []))


def _has_image(result) -> bool:
    return any(getattr(b, "type", "") == "image" for b in (result.content or []))


def _get(world, path: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(world.base + path, timeout=60) as r:
        return r.status, r.read()


def _need(tool_available: bool, what: str) -> None:
    if tool_available:
        return
    if REQUIRE_LATEX:
        pytest.fail(f"{what} is required in this environment (E2E_REQUIRE_LATEX=1) but missing")
    pytest.skip(f"{what} not installed")


# --- the world: two real remotes, one real server on a real port --------------

@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("e2e")
    remote_a = _make_remote(root, "a", {
        "main.tex": MAIN_TEX.encode(),
        "sections/method.tex": METHOD_TEX.encode(),
        "refs.bib": REFS_BIB.encode(),
        "figures/plot.png": FIGURE_BYTES,
    })
    remote_b = _make_remote(root, "b", {
        "main.tex": b"\\documentclass{article}\\begin{document}Paper B.\\end{document}\n",
    })

    store = InMemoryStore()
    cipher = TokenCipher(TokenCipher.generate_key())
    asyncio.run(store.upsert_user(User(user_id="u", email="e2e@example.com", plan="pro")))
    svc = AccountService(store, cipher)
    asyncio.run(svc.connect_project(
        "u", f"https://www.overleaf.com/project/{HEX_A}", "olp_e2e", "paper-a",
        git_url=remote_a.as_uri()))
    asyncio.run(svc.add_project(
        "u", f"https://www.overleaf.com/project/{HEX_B}", "paper-b",
        git_url=remote_b.as_uri()))

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    mcp = create_hosted_server(
        store=store, cipher=cipher, auth=False,
        identity_provider=lambda: ("u", "e2e@example.com"),
        base_url=base, data_dir=root / "cache",
        capacity=CapacityGate(subscription_id="", resource_group="", stripe_api_key=""),
    )
    server = uvicorn.Server(uvicorn.Config(
        mcp.http_app(stateless_http=True), host="127.0.0.1", port=port,
        log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started:
        assert time.time() < deadline, "server did not start"
        time.sleep(0.05)

    yield SimpleNamespace(root=root, remote_a=remote_a, remote_b=remote_b,
                          base=base, mcp_url=f"{base}/mcp")
    server.should_exit = True
    thread.join(timeout=15)


# --- journeys -------------------------------------------------------------------

def test_web_surface_serves(world):
    for path in ("/", "/tools", "/tools/bibtex", "/tools/latex-error-finder",
                 "/privacy", "/terms", "/robots.txt", "/sitemap.xml"):
        status, body = _get(world, path)
        assert status == 200, path
        assert body, path
    status, body = _get(world, "/health/capacity")
    assert status == 200 and json.loads(body)["free_open"] is True


def test_research_journey(world):
    from fastmcp.exceptions import ToolError

    # Discover.
    out = _text(_call(world, "list_projects"))
    assert "paper-a" in out and "paper-b" in out
    out = _text(_call(world, "list_files", {"project": "paper-a"}))
    for f in ("main.tex", "refs.bib", "sections/method.tex"):
        assert f in out
    out = _text(_call(world, "read_file", {
        "path": "main.tex", "project": "paper-a", "with_line_numbers": False}))
    assert "Quantum networks are fragile" in out
    assert "Introduction" in _text(_call(world, "get_sections", {
        "path": "main.tex", "project": "paper-a"}))

    # Checkpoint before editing.
    _call(world, "checkpoint", {"name": "before e2e edits", "project": "paper-a"})
    cps = _text(_call(world, "list_checkpoints", {"project": "paper-a"}))
    cp = cps.split()[0]
    assert re.fullmatch(r"[0-9a-f]{7,40}", cp), cps

    # Edit an existing file and create a new one; both must land on the remote.
    _call(world, "edit_file", {
        "path": "sections/method.tex", "project": "paper-a",
        "old_string": "We measure things carefully.",
        "new_string": "We measure entanglement fidelity carefully."})
    _call(world, "write_file", {
        "path": "sections/results.tex", "project": "paper-a",
        "content": "\\section{Results}\nThe link held.\n"})
    _call(world, "edit_file", {
        "path": "main.tex", "project": "paper-a",
        "old_string": "\\input{sections/method}\n",
        "new_string": "\\input{sections/method}\n\\input{sections/results}\n"})
    assert b"entanglement fidelity" in _on_remote(world, world.remote_a, "sections/method.tex")
    assert b"The link held." in _on_remote(world, world.remote_a, "sections/results.tex")

    # Undo: what changed since the checkpoint, then restore one file from it.
    assert "results.tex" in _text(_call(world, "project_diff", {"ref": cp, "project": "paper-a"}))
    _call(world, "restore_file", {"path": "sections/method.tex", "ref": cp, "project": "paper-a"})
    assert _on_remote(world, world.remote_a, "sections/method.tex") == METHOD_TEX.encode()
    assert "Restore" in _text(_call(world, "get_history", {"project": "paper-a"}))

    # Move a binary figure and a text file from paper-a into paper-b, byte-exact.
    b64 = _text(_call(world, "download_file", {"path": "figures/plot.png", "project": "paper-a"}))
    _call(world, "upload_file", {"path": "imported/plot.png", "content_base64": b64,
                                 "project": "paper-b"})
    assert _on_remote(world, world.remote_b, "imported/plot.png") == FIGURE_BYTES
    bib = _text(_call(world, "read_file", {
        "path": "refs.bib", "project": "paper-a", "with_line_numbers": False}))
    _call(world, "write_file", {"path": "refs.bib", "content": bib, "project": "paper-b"})
    assert _on_remote(world, world.remote_b, "refs.bib") == REFS_BIB.encode()

    # Rename in place; the project keeps working under its new name.
    _call(world, "rename_project", {"project": "paper-b", "new_name": "paper-b renamed"})
    assert "paper-b renamed" in _text(_call(world, "list_projects"))
    assert "imported/plot.png" in _text(_call(world, "list_files", {"project": "paper-b renamed", "all_files": True}))

    # Safety: path traversal is refused over the wire.
    with pytest.raises(ToolError):
        _call(world, "read_file", {"path": "../../etc/passwd", "project": "paper-a"})


def test_latex_journey(world):
    _need(bool(texcompile.tectonic_path()), "Tectonic")
    assert json.loads(_get(world, "/health/capacity")[1])["latex_available"] is True

    out = _text(_call(world, "check_compile", {"project": "paper-a", "tex": "main.tex"}))
    assert "Compiles cleanly" in out, out

    assert _has_image(_call(world, "show_page", {"page": 1, "project": "paper-a"}))

    # arXiv bundle with a REAL bibliography: bibtex must have run for main.bbl
    # to contain the cited entry.
    out = _text(_call(world, "arxiv_export", {"project": "paper-a", "tex": "main.tex"}))
    assert "main.bbl" in out, out
    url = re.search(r"(/dl\?code=\S+)", out).group(1)
    status, blob = _get(world, url)
    assert status == 200
    z = zipfile.ZipFile(io.BytesIO(blob))
    flat = z.read("main.tex").decode()
    assert "\\input{sections/method}" not in flat and "We measure" in flat
    assert "Knuth" in z.read("main.bbl").decode()

    _need(bool(shutil.which("latexdiff")), "latexdiff")
    assert _has_image(_call(world, "tracked_changes_pdf", {"ref": "HEAD~1", "project": "paper-a"}))
