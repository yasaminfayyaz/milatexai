"""Compile result reuse with the REAL LaTeX engine (runs inside the production image in CI,
where E2E_REQUIRE_LATEX=1 makes a missing engine a failure, not a skip).

Proves, with real compiles of real documents:
  - a clean paper is compiled once for check_compile + show_page + show_table + show_figure,
    and check_compile says exactly what the ordinary compile says ("Compiles cleanly (N pages).");
  - a broken paper reports exactly the errors (line numbers and file names included) of the
    ordinary compile of the real document;
  - a preamble that clashes with the page locator's helpers is still reported as compiling;
  - an edit made directly on the remote (as in Overleaf, GitHub or GitLab) shows up at once,
    and so does an edit made through MiLatexAI.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import warnings
from pathlib import Path

import pytest

from leafbridge import texcompile, texlocate
from leafbridge.hosted import create_hosted_server
from leafbridge.service import AccountService
from leafbridge.store import InMemoryStore, TokenCipher, User

warnings.filterwarnings("ignore", category=DeprecationWarning)
REQUIRE_LATEX = os.environ.get("E2E_REQUIRE_LATEX") == "1"
HEX = "abcdefabcdefabcdefabcdef"
BS = chr(92)

CLEAN = (f"{BS}documentclass{{article}}\n{BS}usepackage{{booktabs}}\n{BS}usepackage{{tikz}}\n{BS}begin{{document}}\n"
         f"Intro text.\n{BS}begin{{table}}[h]{BS}centering{BS}caption{{T}}{BS}label{{tab:a}}\n"
         f"{BS}begin{{tabular}}{{lr}}{BS}toprule A & B {BS}{BS} {BS}midrule 1 & 2 {BS}{BS} {BS}bottomrule{BS}end{{tabular}}{BS}end{{table}}\n"
         f"{BS}newpage\n{BS}begin{{figure}}[h]{BS}centering{BS}begin{{tikzpicture}}{BS}draw (0,0) -- (1,1);{BS}end{{tikzpicture}}"
         f"{BS}caption{{F}}{BS}label{{fig:a}}{BS}end{{figure}}\nEnd.\n{BS}end{{document}}\n")
BROKEN = (f"{BS}documentclass{{article}}\n{BS}begin{{document}}\nLine three.\nLine four.\n"
          f"Here comes {BS}notarealcommand an error.\n{BS}end{{document}}\n")
CLASH = (f"{BS}documentclass{{article}}\n{BS}newcounter{{milainst}}\n{BS}begin{{document}}\nFine on its own.\n{BS}end{{document}}\n")


def _need_engine():
    if texcompile.tectonic_path():
        return
    if REQUIRE_LATEX:
        pytest.fail("Tectonic is required here (E2E_REQUIRE_LATEX=1) but missing")
    pytest.skip("Tectonic not installed")


def _git(args, cwd):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, p.stderr or p.stdout
    return p.stdout


def _world(tmp_path: Path, monkeypatch, doc: str):
    remote, seed = tmp_path / "remote.git", tmp_path / "seed"
    remote.mkdir()
    _git(["init", "--bare", "-b", "main", "."], remote)
    seed.mkdir()
    _git(["init", "-b", "main", "."], seed)
    (seed / "main.tex").write_text(doc)
    _git(["add", "-A"], seed)
    _git(["-c", "user.name=S", "-c", "user.email=s@t", "commit", "-m", "init"], seed)
    _git(["remote", "add", "origin", remote.as_uri()], seed)
    _git(["push", "-u", "origin", "main"], seed)

    counts = {"located": 0, "plain": 0}
    real_locate, real_plain = texlocate.compile_and_locate, texcompile.compile_project

    def counting_locate(*a, **k):
        counts["located"] += 1
        return real_locate(*a, **k)

    async def counting_plain(*a, **k):
        counts["plain"] += 1
        return await real_plain(*a, **k)
    monkeypatch.setattr(texlocate, "compile_and_locate", counting_locate)
    monkeypatch.setattr(texcompile, "compile_project", counting_plain)

    store = InMemoryStore()
    cipher = TokenCipher(TokenCipher.generate_key())
    asyncio.run(store.upsert_user(User(user_id="u", email="t@x.com", plan="pro")))
    asyncio.run(AccountService(store, cipher).connect_project(
        "u", f"https://www.overleaf.com/project/{HEX}", "olp_x", "paper", git_url=remote.as_uri()))
    mcp = create_hosted_server(store=store, cipher=cipher, auth=False, identity_provider=lambda: ("u", "t@x.com"),
                               base_url="https://milatexai.com", data_dir=tmp_path / "copy")

    def outside_edit(text: str):
        _git(["pull", "-q", "origin", "main"], seed)
        (seed / "main.tex").write_text(text)
        _git(["-c", "user.name=Web", "-c", "user.email=w@t", "commit", "-qam", "edited on the website"], seed)
        _git(["push", "-q", "origin", "main"], seed)

    return mcp, counts, outside_edit, seed, real_plain


def call(mcp, tool, args):
    from fastmcp import Client

    async def go():
        async with Client(mcp) as c:
            r = await c.call_tool(tool, args, raise_on_error=False)
            return (r, " ".join(getattr(b, "text", "") for b in r.content),
                    any(getattr(b, "type", "") == "image" for b in r.content))
    return asyncio.run(go())


def test_clean_paper_compiles_once_and_reports_what_the_ordinary_compile_reports(tmp_path, monkeypatch):
    _need_engine()
    mcp, counts, _, seed, real_plain = _world(tmp_path, monkeypatch, CLEAN)
    _, out, _ = call(mcp, "check_compile", {"project": "paper"})
    expected = asyncio.run(real_plain(seed, "main.tex"))           # the old path, on the same document
    assert expected.ok and f"main.tex: {expected.message}" in out, (out, expected.message)
    assert "Table 1: p.1" in out and "Figure 1: p.2" in out
    for tool, args in (("show_page", {"page": 2}), ("show_table", {"table": "1"}), ("show_figure", {"figure": "fig:a"})):
        r, text, img = call(mcp, tool, {**args, "project": "paper"})
        assert not r.is_error and img, (tool, text)
    assert counts == {"located": 1, "plain": 0}, counts


def test_errors_are_exactly_the_ordinary_compiles_errors(tmp_path, monkeypatch):
    _need_engine()
    mcp, counts, _, seed, real_plain = _world(tmp_path, monkeypatch, BROKEN)
    _, out, _ = call(mcp, "check_compile", {"project": "paper"})
    expected = asyncio.run(real_plain(seed, "main.tex"))
    assert not expected.ok and expected.errors
    assert "Compile FAILED." in out
    for e in expected.errors:
        assert e in out, (e, out)                                   # same lines, numbers and file names
    assert "__mila" not in out                                      # nothing from the page-locating copy leaks out
    r, text, _ = call(mcp, "show_page", {"page": 1, "project": "paper"})
    assert counts["located"] == 1                                   # the broken version is not compiled again


def test_a_preamble_that_clashes_with_the_locator_still_compiles(tmp_path, monkeypatch):
    _need_engine()
    mcp, counts, _, _, _ = _world(tmp_path, monkeypatch, CLASH)
    _, out, _ = call(mcp, "check_compile", {"project": "paper"})
    assert "Compiles cleanly" in out and "FAILED" not in out, out
    assert counts == {"located": 1, "plain": 1}


def test_edits_made_on_the_website_and_through_milatexai_show_up_at_once(tmp_path, monkeypatch):
    _need_engine()
    one_page = f"{BS}documentclass{{article}}{BS}begin{{document}}One page.{BS}end{{document}}\n"
    mcp, counts, outside_edit, _, _ = _world(tmp_path, monkeypatch, one_page)
    _, out, _ = call(mcp, "show_page", {"page": 1, "project": "paper"})
    assert "Page 1 of 1." in out
    outside_edit(one_page.replace("One page.", f"One page.{BS}newpage Two pages."))   # typed in Overleaf, seconds later
    r, out, img = call(mcp, "show_page", {"page": 2, "project": "paper"})
    assert not r.is_error and img and "Page 2 of 2." in out, out
    r, out, _ = call(mcp, "edit_file", {"path": "main.tex", "old_string": "Two pages.",
                                        "new_string": f"Two pages.{BS}newpage Three.", "project": "paper"})
    assert not r.is_error, out
    r, out, img = call(mcp, "show_page", {"page": 3, "project": "paper"})
    assert not r.is_error and img and "Page 3 of 3." in out, out
    assert counts["located"] == 3
