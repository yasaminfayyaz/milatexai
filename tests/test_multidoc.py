"""Projects that hold several documents (a paper, its manuscript version, an outline).

From an audit on such a project: show_table / show_figure / tracked_changes_pdf could not
target a specific document, project_stats merged every document, auto-detection silently
picked one, \\cite{#1} inside a macro counted as an undefined key, and a push failed once on
a dropped connection ("bad band") during parallel writes.
"""

from __future__ import annotations

import asyncio
import subprocess
import warnings
from pathlib import Path

import pytest
from fastmcp import Client

from leafbridge import citations, paperstats, texcompile, texdiff, texlocate
from leafbridge.config import ProjectConfig
from leafbridge.git_worker import GitError, GitWorker
from leafbridge.hosted import create_hosted_server
from leafbridge.service import AccountService
from leafbridge.store import InMemoryStore, TokenCipher, User

warnings.filterwarnings("ignore", category=DeprecationWarning)
fitz = pytest.importorskip("fitz")
BS = chr(92)


def _doc(body: str) -> str:
    return f"{BS}documentclass{{article}}\n{BS}begin{{document}}\n{body}\n{BS}end{{document}}\n"


FILES = {
    "main.tex": _doc(f"Main paper words here. See {BS}ref{{tab:main}}. {BS}input{{sections/intro}}"),
    "sections/intro.tex": f"Intro words for main. {BS}label{{tab:main}}\n",
    "CRITIS_manuscript.tex": _doc(f"Manuscript words only. {BS}label{{tab:framework-summary}} {BS}ref{{fig:hybrid-flow}}"),
    "CRITIS_outline.tex": _doc("Outline words."),
    "macros.tex": f"{BS}newcommand{{{BS}mycite}}[1]{{{BS}cite{{#1}}}}\n",
}


def _git(args, cwd):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


@pytest.fixture
def world(tmp_path, monkeypatch):
    remote, seed = tmp_path / "r.git", tmp_path / "seed"
    remote.mkdir()
    _git(["init", "-q", "--bare", "-b", "main", "."], remote)
    seed.mkdir()
    _git(["init", "-q", "-b", "main", "."], seed)
    for rel, text in FILES.items():
        (seed / rel).parent.mkdir(parents=True, exist_ok=True)
        (seed / rel).write_text(text)
    _git(["add", "-A"], seed)
    _git(["-c", "user.name=S", "-c", "user.email=s@t", "commit", "-qm", "v1"], seed)
    _git(["push", "-q", remote.as_uri(), "main"], seed)
    store, cipher = InMemoryStore(), TokenCipher(TokenCipher.generate_key())
    asyncio.run(store.upsert_user(User(user_id="u", email="u@x.com", plan="pro")))
    asyncio.run(AccountService(store, cipher).connect_project(
        "u", "https://www.overleaf.com/project/0123456789abcdef01234567", "olp_x", "paper", git_url=remote.as_uri()))

    compiled = []

    def fake_locate(repo_dir, main, exe, cache_dir=None):
        compiled.append(main)
        pdf = Path(repo_dir) / (Path(main).stem + "__mila.pdf")
        doc = fitz.open()
        doc.new_page()
        doc.save(str(pdf))
        num = "1" if main == "main.tex" else "7"
        return texlocate.LocateResult(ok=True, floats={("table", num): texlocate.Float("table", num, 1, 1)},
                                      labels={}, pdf_path=str(pdf), message="ok", clean=True, pages=1)
    monkeypatch.setattr(texcompile, "tectonic_path", lambda: "/fake/tectonic")
    monkeypatch.setattr(texlocate, "compile_and_locate", fake_locate)
    mcp = create_hosted_server(store=store, cipher=cipher, auth=False, identity_provider=lambda: ("u", "u@x.com"),
                               base_url="https://milatexai.com", data_dir=tmp_path / "data")
    return mcp, compiled


def _call(mcp, tool, args):
    async def go():
        async with Client(mcp) as c:
            r = await c.call_tool(tool, args, raise_on_error=False)
            return r.is_error, " ".join(getattr(b, "text", "") for b in r.content if getattr(b, "type", "") == "text")
    return asyncio.run(go())


def test_every_document_in_the_project_is_found(tmp_path):
    for rel, text in FILES.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    assert texcompile.root_documents(tmp_path) == ["CRITIS_manuscript.tex", "CRITIS_outline.tex", "main.tex"]


def test_show_table_can_target_another_document(world):
    mcp, compiled = world
    err, out = _call(mcp, "show_table", {"table": "7", "tex": "CRITIS_manuscript.tex"})
    assert not err and "Table 7: page 1." in out and compiled[-1] == "CRITIS_manuscript.tex"
    assert "this project has" not in out                                    # chosen explicitly: no hint


def test_without_tex_the_tools_say_which_document_they_used(world):
    mcp, _ = world
    err, out = _call(mcp, "show_table", {"table": "1"})
    assert not err and "this used main.tex" in out and "CRITIS_manuscript.tex" in out
    err, out = _call(mcp, "check_compile", {})
    assert "this used main.tex" in out and "CRITIS_outline.tex" in out


def test_tracked_changes_can_target_another_document(world, monkeypatch):
    mcp, _ = world
    seen = {}

    async def fake_diff(repo, main, old, timeout=240):
        seen["main"] = main
        doc = fitz.open()
        doc.new_page()
        return doc.tobytes()
    monkeypatch.setattr(texdiff, "diff_pdf", fake_diff)
    err, out = _call(mcp, "tracked_changes_pdf", {"ref": "HEAD", "tex": "CRITIS_manuscript.tex"})
    assert not err and seen["main"] == "CRITIS_manuscript.tex"


def test_project_stats_counts_each_document_separately(world):
    mcp, _ = world
    err, out = _call(mcp, "project_stats", {})
    assert not err and "3 documents in this project" in out
    before, main_block = out.split("\nmain.tex: ~")                       # the block headed by main.tex
    assert "sections/intro.tex" in main_block                               # its \input counts with it
    assert "sections/intro.tex" not in before                               # and not with the others
    # tab:main is defined in main's own \input: not undefined there. fig:hybrid-flow only in the manuscript.
    assert "Undefined \\ref targets: fig:hybrid-flow" in out and "tab:main" not in out.split("Undefined")[1]
    err, out = _call(mcp, "project_stats", {"tex": "CRITIS_outline.tex"})
    assert "documents in this project" not in out and "CRITIS_outline.tex: ~" in out


def test_a_cite_inside_a_macro_definition_is_not_a_key():
    assert citations.cite_keys(f"{BS}newcommand{{{BS}c}}[1]{{{BS}cite{{#1}}}} {BS}cite{{real,#2}}") == {"real"}


def test_a_dropped_connection_during_a_push_is_retried(tmp_path, monkeypatch):
    worker = GitWorker(tmp_path / "w")
    calls = {"n": 0}

    async def flaky(project, args, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise GitError("send-pack: protocol error: bad band #69\nfatal: the remote end hung up unexpectedly")
        return "ok"
    monkeypatch.setattr(worker, "_git", flaky)
    monkeypatch.setattr("leafbridge.git_worker.RETRY_DELAYS", (0.0, 0.0, 0.0))
    cfg = ProjectConfig(name="p", project_id="0123456789abcdef01234567", token="olp_testtoken_123")
    assert asyncio.run(worker._git_networked(cfg, ["push"])) == "ok" and calls["n"] == 2
