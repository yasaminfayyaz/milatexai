"""Tests for tracked-changes PDFs (texdiff) and arXiv export (arxivprep + /dl)."""

from __future__ import annotations

import asyncio
import io
import subprocess
import warnings
import zipfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from leafbridge import arxivprep, texdiff
from leafbridge.hosted import create_hosted_server
from leafbridge.store import InMemoryStore, TokenCipher, User

warnings.filterwarnings("ignore", category=DeprecationWarning)

HEX = "0123456789abcdef01234567"
OVERLEAF_URL = f"https://www.overleaf.com/project/{HEX}"


# --- arxivprep unit ---------------------------------------------------------

def test_flatten_inlines_recursively_and_strips_comments(tmp_path: Path):
    (tmp_path / "main.tex").write_text(
        "\\documentclass{article}\n% top comment\n\\begin{document}\n"
        "\\input{sections/intro}\nEnd.\n\\end{document}\n")
    (tmp_path / "sections").mkdir()
    (tmp_path / "sections" / "intro.tex").write_text(
        "Intro text.\n% inner comment\n\\input{sections/intro}\n")  # circular!
    flat = arxivprep.flatten(tmp_path, "main.tex")
    assert "Intro text." in flat and "End." in flat
    assert "% top comment" not in flat and "% inner comment" not in flat
    assert "circular" in flat  # cycle guarded, not infinite


def test_flatten_missing_file_noted(tmp_path: Path):
    (tmp_path / "main.tex").write_text("\\input{nowhere}\n")
    assert "missing file" in arxivprep.flatten(tmp_path, "main.tex")


def test_referenced_graphics_resolves_extensions(tmp_path: Path):
    (tmp_path / "figures").mkdir()
    (tmp_path / "figures" / "a.png").write_bytes(b"x")
    (tmp_path / "figures" / "b.pdf").write_bytes(b"x")
    flat = "\\includegraphics[width=5cm]{figures/a}\\includegraphics{figures/b.pdf}\\includegraphics{figures/ghost}"
    assert arxivprep.referenced_graphics(flat, tmp_path) == ["figures/a.png", "figures/b.pdf"]


def test_build_zip_contents(tmp_path: Path):
    (tmp_path / "main.tex").write_text(
        "\\documentclass{x}\\begin{document}\\includegraphics{fig1}"
        "\\bibliography{refs}\\end{document}\n")
    (tmp_path / "fig1.png").write_bytes(b"img")
    (tmp_path / "custom.cls").write_text("cls")
    blob, manifest = arxivprep.build_zip(tmp_path, "main.tex", "BBL CONTENT")
    z = zipfile.ZipFile(io.BytesIO(blob))
    names = set(z.namelist())
    assert {"main.tex", "main.bbl", "fig1.png", "custom.cls"} <= names
    flat = z.read("main.tex").decode()
    assert "\\input{main.bbl}" in flat and "\\bibliography{refs}" not in flat
    assert z.read("main.bbl") == b"BBL CONTENT"
    assert "main.bbl (precompiled bibliography)" in manifest


def test_build_zip_without_bbl(tmp_path: Path):
    (tmp_path / "main.tex").write_text("\\bibliography{refs}\n")
    blob, manifest = arxivprep.build_zip(tmp_path, "main.tex", None)
    z = zipfile.ZipFile(io.BytesIO(blob))
    assert "main.bbl" not in z.namelist()
    assert "\\bibliography{refs}" in z.read("main.tex").decode()  # untouched


# --- texdiff real pipeline (skipped without local perl+latexdiff+tectonic) ---

def _latexdiff_available():
    return texdiff.latexdiff_cmd() is not None


@pytest.mark.skipif(not _latexdiff_available(), reason="no latexdiff")
def test_real_latexdiff_pdf(tmp_path: Path):
    import shutil
    if not (shutil.which("tectonic") or __import__("os").environ.get("LEAFBRIDGE_TECTONIC")):
        pytest.skip("no tectonic")
    (tmp_path / "main.tex").write_text(
        "\\documentclass{article}\\begin{document}New improved words. Same.\\end{document}\n")
    old = "\\documentclass{article}\\begin{document}Old words. Same.\\end{document}\n"
    pdf = asyncio.run(texdiff.diff_pdf(tmp_path, "main.tex", old))
    assert pdf.startswith(b"%PDF")
    assert texdiff.pdf_pages_to_pngs(pdf)[0][:8] == b"\x89PNG\r\n\x1a\n"
    # temp files cleaned
    assert not (tmp_path / "__mila_old.tex").exists()
    assert not (tmp_path / "__mila_diff.tex").exists()


# --- tool level -------------------------------------------------------------

def _git(args, cwd):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout


def _harness(tmp_path: Path):
    remote = tmp_path / "remote.git"; seed = tmp_path / "seed"
    remote.mkdir(parents=True); _git(["init", "--bare", "-b", "main", "."], remote)
    seed.mkdir(parents=True); _git(["init", "-b", "main", "."], seed)
    (seed / "main.tex").write_text(
        "\\documentclass{article}\\begin{document}Words.\\end{document}\n")
    _git(["add", "-A"], seed)
    _git(["-c", "user.name=S", "-c", "user.email=s@t", "commit", "-m", "init"], seed)
    _git(["remote", "add", "origin", remote.as_uri()], seed)
    _git(["push", "-u", "origin", "main"], seed)
    store = InMemoryStore()
    cipher = TokenCipher(TokenCipher.generate_key())
    asyncio.run(store.upsert_user(User(user_id="u", email="t@x.com", plan="pro")))
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


def test_tracked_changes_tool_returns_images(tmp_path, monkeypatch):
    mcp = _harness(tmp_path)
    pdf = _fake_pdf()

    async def fake_diff(repo, main, old, timeout=240):
        assert "Words." in open(Path(repo) / main, encoding="utf-8").read()
        return pdf
    monkeypatch.setattr(texdiff, "diff_pdf", fake_diff)
    r = _call(mcp, "tracked_changes_pdf", {"ref": "HEAD"})
    assert "Tracked changes" in _text(r)
    assert any("Image" in type(b).__name__ for b in r.content)


def test_arxiv_export_and_download_roundtrip(tmp_path, monkeypatch):
    mcp = _harness(tmp_path)

    async def fake_bbl(repo, main, timeout=240):
        return "THE BBL"
    monkeypatch.setattr(arxivprep, "compile_bbl", fake_bbl)
    out = _text(_call(mcp, "arxiv_export", {}))
    assert "main.tex (flattened)" in out and "main.bbl" in out
    assert "/dl?code=" in out
    url = next(l for l in out.splitlines() if "/dl?code=" in l).split(": ", 1)[1].strip()
    path_q = url.split("milatexai.com", 1)[1]
    with TestClient(mcp.http_app(), base_url="https://testserver") as client:
        resp = client.get(path_q)
        assert resp.status_code == 200
        z = zipfile.ZipFile(io.BytesIO(resp.content))
        assert "main.tex" in z.namelist() and z.read("main.bbl") == b"THE BBL"
        # Bad code -> clean expiry page.
        assert client.get("/dl?code=garbage").status_code == 400
