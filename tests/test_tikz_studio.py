"""TikZ Studio tests: wrapping, %-headers, the commit_tikz tool, and (when a
local Tectonic is available) a REAL standalone compile."""

from __future__ import annotations

import asyncio
import subprocess
import warnings
from pathlib import Path

import pytest

from leafbridge import figures, tikz
from leafbridge.hosted import create_hosted_server
from leafbridge.store import InMemoryStore, TokenCipher, User
from leafbridge.texcompile import tectonic_path

warnings.filterwarnings("ignore", category=DeprecationWarning)

HEX = "0123456789abcdef01234567"
OVERLEAF_URL = f"https://www.overleaf.com/project/{HEX}"
SNIPPET = "\\draw[->] (0,0) -- (2,1) node[right] {$x$};"


def test_wrap_variants():
    w = tikz.wrap(SNIPPET)
    assert "standalone" in w and "\\begin{tikzpicture}" in w
    already = "\\begin{tikzpicture}\n\\draw (0,0) circle (1);\n\\end{tikzpicture}"
    assert tikz.wrap(already).count("tikzpicture") == 2 + 0  # not double-wrapped
    full = "\\documentclass{standalone}\\begin{document}x\\end{document}"
    assert tikz.wrap(full) == full  # idempotent on full docs


def test_percent_header_round_trip_and_scan(tmp_path: Path):
    body = SNIPPET + "\n"
    head = figures.build_header("flow", code_body=body, pdf_bytes=b"%PDF-x",
                                ext="png", comment="%")
    assert head.startswith("% === milatexai figure ===")
    text = head + body
    parsed = figures.parse_header(text)
    assert parsed["figure"] == "flow" and parsed["output"] == "figures/flow.png"
    assert figures.split_body(text) == body
    # scan_figures picks up .tex sources too.
    src = tmp_path / "figures" / "src"
    src.mkdir(parents=True)
    (src / "flow.tex").write_text(text, encoding="utf-8")
    (tmp_path / "figures" / "flow.png").write_bytes(b"%PDF-x")
    found = figures.scan_figures(tmp_path)
    assert [f.slug for f in found] == ["flow"]
    assert figures.sync_state(tmp_path, found[0]) == figures.IN_SYNC


# --- tool-level -------------------------------------------------------------

def _git(args, cwd):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout


def _harness(tmp_path: Path, *, plan="pro"):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    remote.mkdir(parents=True); _git(["init", "--bare", "-b", "main", "."], remote)
    seed.mkdir(parents=True); _git(["init", "-b", "main", "."], seed)
    (seed / "main.tex").write_text("x\n")
    _git(["add", "-A"], seed)
    _git(["-c", "user.name=S", "-c", "user.email=s@t", "commit", "-m", "init"], seed)
    _git(["remote", "add", "origin", remote.as_uri()], seed)
    _git(["push", "-u", "origin", "main"], seed)
    store = InMemoryStore()
    cipher = TokenCipher(TokenCipher.generate_key())
    asyncio.run(store.upsert_user(User(user_id="u", email="t@x.com", plan=plan)))
    mcp = create_hosted_server(
        store=store, cipher=cipher, auth=False,
        identity_provider=lambda: ("u", "t@x.com"),
        base_url="https://milatexai.com", data_dir=tmp_path / "cache",
    )
    from leafbridge.service import AccountService

    asyncio.run(AccountService(store, cipher).connect_project(
        "u", OVERLEAF_URL, "olp_x", "paper", git_url=remote.as_uri()))
    return mcp


def _call(mcp, tool, args):
    from fastmcp import Client

    async def go():
        async with Client(mcp) as c:
            return await c.call_tool(tool, args)
    return asyncio.run(go())


def _text(r):
    return "".join(getattr(b, "text", "") for b in (r.content or []))


def _fake_pdf() -> bytes:
    import fitz
    d = fitz.open(); d.new_page(width=100, height=80)
    return d.tobytes()


def test_commit_tikz_commits_source_and_png(tmp_path, monkeypatch):
    mcp = _harness(tmp_path)
    pdf = _fake_pdf()

    async def fake_render(code, timeout=150):
        return pdf
    monkeypatch.setattr(tikz, "render_pdf", fake_render)
    r = _call(mcp, "commit_tikz", {"code": SNIPPET, "name": "flow chart"})
    text = _text(r)
    assert "figures/src/flow-chart.tex" in text and "figures/flow-chart.png" in text
    verify = tmp_path / "v"
    _git(["-c", "core.autocrlf=false", "clone", (tmp_path / "remote.git").as_uri(), str(verify)], tmp_path)
    src = (verify / "figures" / "src" / "flow-chart.tex").read_text()
    assert "milatexai figure" not in src and SNIPPET in src
    assert (verify / "figures" / "flow-chart.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert any("Image" in type(b).__name__ for b in r.content)


def test_commit_tikz_free_user_gated_and_error_surfaced(tmp_path, monkeypatch):
    from fastmcp.exceptions import ToolError

    mcp = _harness(tmp_path, plan="free")
    with pytest.raises(ToolError, match="Pro feature"):
        _call(mcp, "commit_tikz", {"code": SNIPPET, "name": "x"})
    mcp2 = _harness(tmp_path / "b")

    async def bad_render(code, timeout=150):
        raise tikz.TikzError("TikZ failed to compile:\n! Undefined control sequence.")
    monkeypatch.setattr(tikz, "render_pdf", bad_render)
    with pytest.raises(ToolError, match="Undefined control sequence"):
        _call(mcp2, "commit_tikz", {"code": "\\badcmd", "name": "y"})


@pytest.mark.skipif(not tectonic_path(), reason="no local tectonic engine")
def test_real_tikz_render():
    pdf = asyncio.run(tikz.render_pdf(SNIPPET))
    assert pdf.startswith(b"%PDF")
    png = figures.pdf_to_png(pdf)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
