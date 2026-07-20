"""Tests for version-safety tools, the citation toolkit, and project stats."""

from __future__ import annotations

import asyncio
import subprocess
import warnings
from pathlib import Path

import pytest

from leafbridge import citations, paperstats
from leafbridge.citations import CitationError
from leafbridge.hosted import create_hosted_server
from leafbridge.store import InMemoryStore, TokenCipher, User

warnings.filterwarnings("ignore", category=DeprecationWarning)

HEX = "0123456789abcdef01234567"
OVERLEAF_URL = f"https://www.overleaf.com/project/{HEX}"


# --- citations.py unit ------------------------------------------------------

def test_classify_doi_and_arxiv():
    assert citations.classify("10.1000/xyz123") == ("doi", "10.1000/xyz123")
    assert citations.classify("https://doi.org/10.1000/xyz123")[0] == "doi"
    assert citations.classify("2301.01234") == ("arxiv", "2301.01234")
    assert citations.classify("arXiv:2301.01234v2")[1] == "2301.01234v2"
    with pytest.raises(CitationError):
        citations.classify("Smith et al 2021")


def test_bib_and_cite_key_parsing():
    bib = "@article{smith21,\n title={X}\n}\n@string{me = {Me}}\n@book{doe19, title={Y}}\n"
    assert citations.bib_keys(bib) == {"smith21", "doe19"}
    assert citations.entry_key("@misc{abc-1, note={n}}") == "abc-1"
    tex = r"See \cite{smith21, doe19} and \citep[p.~3]{roe20} plus \textcite{poe18}."
    assert citations.cite_keys(tex) == {"smith21", "doe19", "roe20", "poe18"}


# --- paperstats.py unit -----------------------------------------------------

def test_word_count_strips_noise():
    tex = ("% comment words words\n\\section{Intro} Real words here. "
           "$x^2+y$ \\begin{equation}a=b\\end{equation} \\textbf{bold} done.")
    n = paperstats.word_count(tex)
    assert 4 <= n <= 9  # 'Real words here EQN bold done'-ish, never counts comments


def test_analyze_todos_and_refs(tmp_path: Path):
    (tmp_path / "a.tex").write_text(
        "\\label{sec:x} See \\ref{sec:x} and \\ref{fig:missing}. % TODO fix me\n")
    a = paperstats.analyze(tmp_path)
    assert a["undefined_refs"] == ["fig:missing"]
    assert a["counts"] and a["total"] > 0
    assert any("TODO" in t for t in a["todos"])


# --- tool harness -----------------------------------------------------------

def _git(args, cwd):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return proc.stdout


def _harness(tmp_path: Path):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    remote.mkdir()
    _git(["init", "--bare", "-b", "main", "."], remote)
    seed.mkdir()
    _git(["init", "-b", "main", "."], seed)
    (seed / "main.tex").write_text(
        "\\documentclass{article}\\begin{document}Hello \\cite{smith21} "
        "\\bibliography{refs}\\end{document}\n")
    (seed / "refs.bib").write_text("@article{smith21, title={S}}\n")
    _git(["add", "-A"], seed)
    _git(["-c", "user.name=S", "-c", "user.email=s@t", "commit", "-m", "init"], seed)
    _git(["remote", "add", "origin", remote.as_uri()], seed)
    _git(["push", "-u", "origin", "main"], seed)

    store = InMemoryStore()
    cipher = TokenCipher(TokenCipher.generate_key())
    uid = "user_tk"
    asyncio.run(store.upsert_user(User(user_id=uid, email="t@x.com", plan="pro")))
    mcp = create_hosted_server(
        store=store, cipher=cipher, auth=False,
        identity_provider=lambda: (uid, "t@x.com"),
        base_url="https://milatexai.com", data_dir=tmp_path / "cache",
    )
    from leafbridge.service import AccountService

    asyncio.run(AccountService(store, cipher).connect_project(
        uid, OVERLEAF_URL, "olp_x", "paper", git_url=remote.as_uri()))
    return mcp


def _call(mcp, tool, args):
    from fastmcp import Client

    async def go():
        async with Client(mcp) as client:
            return await client.call_tool(tool, args)

    return asyncio.run(go())


def _text(r):
    return "".join(getattr(b, "text", "") for b in (r.content or []))


# --- version safety end-to-end ---------------------------------------------

def test_checkpoint_diff_restore_roundtrip(tmp_path):
    mcp = _harness(tmp_path)
    out = _text(_call(mcp, "checkpoint", {"name": "before rewrite"}))
    assert "before rewrite" in out
    lst = _text(_call(mcp, "list_checkpoints", {}))
    assert "CHECKPOINT: before rewrite" in lst
    ref = lst.split()[0]  # newest checkpoint's short hash
    # Mangle the file, then diff + restore.
    _call(mcp, "edit_file", {"path": "main.tex", "old_string": "Hello",
                             "new_string": "RUINED"})
    assert "main.tex" in _text(_call(mcp, "project_diff", {"ref": ref}))
    _call(mcp, "restore_file", {"path": "main.tex", "ref": ref})
    r = _text(_call(mcp, "read_file", {"path": "main.tex"}))
    assert "Hello" in r and "RUINED" not in r


def test_project_diff_no_changes(tmp_path):
    mcp = _harness(tmp_path)
    lst = _text(_call(mcp, "checkpoint", {"name": "x"}))
    ref = _text(_call(mcp, "list_checkpoints", {})).split()[0]
    assert "No changes" in _text(_call(mcp, "project_diff", {"ref": ref}))


# --- citations end-to-end ---------------------------------------------------

FAKE_BIB = "@article{doe_2024_new,\n  title={New},\n  doi={10.1/abc}\n}"


def test_add_citation_appends_and_dedupes(tmp_path, monkeypatch):
    mcp = _harness(tmp_path)

    async def fake_fetch(ref):
        return FAKE_BIB

    monkeypatch.setattr(citations, "fetch_bibtex", fake_fetch)
    out = _text(_call(mcp, "add_citation", {"reference": "10.1/abc"}))
    assert "doe_2024_new" in out and "refs.bib" in out
    bib = _text(_call(mcp, "read_file", {"path": "refs.bib", "with_line_numbers": False}))
    assert "doe_2024_new" in bib and "smith21" in bib
    # Second add of the same key: no duplicate.
    out2 = _text(_call(mcp, "add_citation", {"reference": "10.1/abc"}))
    assert "already" in out2
    bib2 = _text(_call(mcp, "read_file", {"path": "refs.bib", "with_line_numbers": False}))
    assert bib2.count("doe_2024_new") == 1


def test_add_citation_bad_reference(tmp_path):
    mcp = _harness(tmp_path)
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="DOI"):
        _call(mcp, "add_citation", {"reference": "some paper title"})


def test_check_citations_reports_undefined_and_unused(tmp_path, monkeypatch):
    mcp = _harness(tmp_path)

    async def fake_fetch(ref):
        return FAKE_BIB

    monkeypatch.setattr(citations, "fetch_bibtex", fake_fetch)
    _call(mcp, "add_citation", {"reference": "10.1/abc"})  # unused entry
    _call(mcp, "edit_file", {"path": "main.tex", "old_string": "Hello",
                             "new_string": "Hello \\cite{ghost99}"})
    out = _text(_call(mcp, "check_citations", {}))
    assert "ghost99" in out and "UNDEFINED" in out
    assert "doe_2024_new" in out  # unused


def test_project_stats_tool(tmp_path):
    mcp = _harness(tmp_path)
    out = _text(_call(mcp, "project_stats", {}))
    assert "main.tex" in out and "words" in out
