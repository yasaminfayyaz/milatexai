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


def _harness_two_tex(tmp_path: Path):
    """A project with TWO valid root documents: a conventionally-named
    'main.tex' (which auto-detect always prefers) and a custom-named
    'realpaper.tex' (the one the user actually cares about). Reproduces the
    bug where arxiv_export silently flattened the wrong document with no way
    to point it at the right one."""
    remote = tmp_path / "remote.git"; seed = tmp_path / "seed"
    remote.mkdir(parents=True); _git(["init", "--bare", "-b", "main", "."], remote)
    seed.mkdir(parents=True); _git(["init", "-b", "main", "."], seed)
    (seed / "main.tex").write_text(
        "\\documentclass{article}\\begin{document}Wrong document.\\end{document}\n")
    (seed / "realpaper.tex").write_text(
        "\\documentclass{article}\\begin{document}The real paper.\\end{document}\n")
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


def test_arxiv_export_auto_detect_prefers_conventional_name(tmp_path, monkeypatch):
    """Reproduces the reported bug: with two valid documents, auto-detection
    picks the conventionally-named one (main.tex) over the user's actual
    paper, silently."""
    mcp = _harness_two_tex(tmp_path)

    async def fake_bbl(repo, main, timeout=240):
        return "BBL FOR " + main
    monkeypatch.setattr(arxivprep, "compile_bbl", fake_bbl)
    out = _text(_call(mcp, "arxiv_export", {}))
    assert "main.tex (flattened)" in out
    url = next(l for l in out.splitlines() if "/dl?code=" in l).split(": ", 1)[1].strip()
    path_q = url.split("milatexai.com", 1)[1]
    with TestClient(mcp.http_app(), base_url="https://testserver") as client:
        z = zipfile.ZipFile(io.BytesIO(client.get(path_q).content))
        flattened = z.read("main.tex").decode()
    assert "Wrong document." in flattened
    assert "The real paper." not in flattened


def test_arxiv_export_tex_override_targets_the_right_file(tmp_path, monkeypatch):
    """The fix: passing tex= exports the SPECIFIED document, regardless of
    what auto-detection would have picked."""
    mcp = _harness_two_tex(tmp_path)

    async def fake_bbl(repo, main, timeout=240):
        return "BBL FOR " + main
    monkeypatch.setattr(arxivprep, "compile_bbl", fake_bbl)
    out = _text(_call(mcp, "arxiv_export", {"tex": "realpaper.tex"}))
    # The zip's internal entry is always named main.tex (arXiv's expected
    # layout); what changes with the override is which SOURCE got flattened.
    assert "main.tex (flattened)" in out
    url = next(l for l in out.splitlines() if "/dl?code=" in l).split(": ", 1)[1].strip()
    path_q = url.split("milatexai.com", 1)[1]
    with TestClient(mcp.http_app(), base_url="https://testserver") as client:
        z = zipfile.ZipFile(io.BytesIO(client.get(path_q).content))
        flattened = z.read("main.tex").decode()
    assert "The real paper." in flattened
    assert "Wrong document." not in flattened


def test_arxiv_export_bad_tex_override_lists_available_files(tmp_path):
    mcp = _harness_two_tex(tmp_path)
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as exc_info:
        _call(mcp, "arxiv_export", {"tex": "nonexistent.tex"})
    msg = str(exc_info.value)
    assert "main.tex" in msg and "realpaper.tex" in msg


# --- download_file: copy a file from one project into another -------------

HEX2_DL = "2223456789abcdef01234567"
OVERLEAF_URL2_DL = f"https://www.overleaf.com/project/{HEX2_DL}"


def _harness_two_projects(tmp_path: Path):
    """Two SEPARATE projects (separate git remotes) connected to the SAME
    user, so a file can be downloaded from one and uploaded into the other."""
    def _make_remote(name: str, tex_content: str):
        remote = tmp_path / f"remote_{name}.git"
        seed = tmp_path / f"seed_{name}"
        remote.mkdir(parents=True); _git(["init", "--bare", "-b", "main", "."], remote)
        seed.mkdir(parents=True); _git(["init", "-b", "main", "."], seed)
        (seed / "main.tex").write_text(tex_content)
        (seed / "figures").mkdir()
        (seed / "figures" / "plot.png").write_bytes(b"\x89PNG fake bytes for project " + name.encode())
        _git(["add", "-A"], seed)
        _git(["-c", "user.name=S", "-c", "user.email=s@t", "commit", "-m", "init"], seed)
        _git(["remote", "add", "origin", remote.as_uri()], seed)
        _git(["push", "-u", "origin", "main"], seed)
        return remote

    remote_a = _make_remote("a", "\\documentclass{article}\\begin{document}Project A.\\end{document}\n")
    remote_b = _make_remote("b", "\\documentclass{article}\\begin{document}Project B.\\end{document}\n")

    store = InMemoryStore()
    cipher = TokenCipher(TokenCipher.generate_key())
    asyncio.run(store.upsert_user(User(user_id="u", email="t@x.com", plan="pro")))
    mcp = create_hosted_server(
        store=store, cipher=cipher, auth=False,
        identity_provider=lambda: ("u", "t@x.com"),
        base_url="https://milatexai.com", data_dir=tmp_path / "cache",
    )
    from leafbridge.service import AccountService

    svc = AccountService(store, cipher)
    asyncio.run(svc.connect_project("u", OVERLEAF_URL, "olp_x", "project-a", git_url=remote_a.as_uri()))
    asyncio.run(svc.add_project("u", OVERLEAF_URL2_DL, "project-b", git_url=remote_b.as_uri()))
    return mcp


def test_download_file_then_upload_into_another_project_byte_identical(tmp_path):
    mcp = _harness_two_projects(tmp_path)

    # Download the figure from project A.
    b64 = _text(_call(mcp, "download_file", {"path": "figures/plot.png", "project": "project-a"}))

    # Upload that same content into project B, under a new name.
    out = _text(_call(mcp, "upload_file", {
        "path": "imported/plot_from_a.png", "content_base64": b64, "project": "project-b",
    }))
    assert "Committed" in out

    # Independently verify: push the remote's contents and compare raw bytes.
    verify = tmp_path / "verify_b"
    _git(["clone", (tmp_path / "remote_b.git").as_uri(), str(verify)], tmp_path)
    landed = (verify / "imported" / "plot_from_a.png").read_bytes()
    original = (tmp_path / "seed_a" / "figures" / "plot.png").read_bytes()
    assert landed == original


def test_download_file_text_also_works(tmp_path):
    mcp = _harness_two_projects(tmp_path)
    b64 = _text(_call(mcp, "download_file", {"path": "main.tex", "project": "project-a"}))
    import base64 as _b64mod
    decoded = _b64mod.b64decode(b64).decode("utf-8")
    assert "Project A." in decoded


def test_download_file_missing_path_errors_clearly(tmp_path):
    from fastmcp.exceptions import ToolError

    mcp = _harness_two_projects(tmp_path)
    with pytest.raises(ToolError) as exc_info:
        _call(mcp, "download_file", {"path": "no/such/file.png", "project": "project-a"})
    assert "No such file" in str(exc_info.value)


def test_download_file_rejects_path_traversal(tmp_path):
    from fastmcp.exceptions import ToolError

    mcp = _harness_two_projects(tmp_path)
    with pytest.raises(ToolError):
        _call(mcp, "download_file", {"path": "../../etc/passwd", "project": "project-a"})
