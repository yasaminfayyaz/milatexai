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


def _thesis_doc() -> str:
    """A report with three chapters, one table each: Tables 1.1, 2.1 and 3.1."""
    parts = [f"{BS}documentclass{{report}}", f"{BS}usepackage{{booktabs}}", f"{BS}usepackage{{xltabular}}",
             f"{BS}begin{{document}}"]
    for n, name in ((1, "one"), (2, "two"), (3, "three")):
        parts += [f"{BS}chapter{{Chapter {name}}}", f"Text of chapter {name}.",
                  f"{BS}begin{{table}}[h]{BS}centering{BS}caption{{Table of chapter {name}}}{BS}label{{tab:{name}}}",
                  f"{BS}begin{{tabular}}{{lr}}{BS}toprule A & B {BS}{BS} {BS}midrule {n} & {n} {BS}{BS} {BS}bottomrule{BS}end{{tabular}}",
                  f"{BS}end{{table}}"]
    # A long table of the xltabular kind (numbered 3.2) and a table with no caption (no number).
    parts += [f"{BS}begin{{xltabular}}{{{BS}linewidth}}{{lX}}{BS}caption{{Long table}}{BS}label{{tab:long}}{BS}{BS}",
              f"A & B {BS}{BS}", f"{BS}end{{xltabular}}",
              f"{BS}begin{{table}}[h]{BS}centering{BS}begin{{tabular}}{{ll}}x & y{BS}end{{tabular}}{BS}end{{table}}"]
    parts.append(f"{BS}end{{document}}")
    return "\n".join(parts) + "\n"


def test_a_thesis_numbered_by_chapter_resolves_every_table_and_label(tmp_path, monkeypatch):
    _need_engine()
    mcp, _, _, _, _ = _world(tmp_path, monkeypatch, _thesis_doc())
    _, out, _ = call(mcp, "check_compile", {"project": "paper"})
    assert "Table 1.1" in out and "Table 2.1" in out and "Table 3.1" in out and "Table 3.2" in out, out
    assert "Table 3.0" not in out and "Table 0" not in out, out           # the uncaptioned table
    for ref in ("2.1", "Table 3.1", "tab:one", "tab:two", "tab:three", "tab:long", "3.2"):
        r, text, img = call(mcp, "show_table", {"table": ref, "project": "paper"})
        assert not r.is_error and img, (ref, text)
    # A bare "1" matches 1.1, 2.1 and 3.1: the tool lists them instead of guessing.
    r, text, img = call(mcp, "show_table", {"table": "1", "project": "paper"})
    assert not img and "Table 2.1" in text and "tab:two" in text, text


def _png(path: Path, w: int, h: int) -> None:
    """A real RGB PNG, one colour (tiny on disk), with no recorded resolution (LaTeX assumes 72 dpi)."""
    import struct
    import zlib

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = (b"\x00" + bytes([90]) * (w * 3)) * h
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))


def test_a_huge_image_is_previewed_from_a_smaller_copy_at_exactly_the_same_size(tmp_path):
    _need_engine()
    import shutil
    import fitz

    repo = tmp_path / "paper"
    repo.mkdir()
    _png(repo / "big.png", 9000, 5000)                    # 45 megapixels: above the limit
    (repo / "main.tex").write_text("\n".join([
        f"{BS}documentclass{{article}}", f"{BS}usepackage{{graphicx}}",
        f"{BS}usepackage[paperwidth=140in,paperheight=90in,margin=1in]{{geometry}}",
        f"{BS}begin{{document}}", f"{BS}noindent{BS}includegraphics{{big.png}}",          # natural size
        f"{BS}newpage", f"{BS}noindent{BS}includegraphics[width=3in]{{big.png}}",         # set width
        f"{BS}end{{document}}", ""]))
    reference = tmp_path / "reference"                   # the original image, compiled directly
    shutil.copytree(repo, reference)
    done = subprocess.run([texcompile.tectonic_path(), "-X", "compile", "main.tex"], cwd=reference,
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stderr[-800:]

    res = texlocate.compile_and_locate(str(repo), "main.tex", texcompile.tectonic_path())
    assert res.clean and "smaller copy" in res.note, res.message

    def placed(pdf):
        doc = fitz.open(str(pdf))
        return [(i["width"], i["bbox"][2] - i["bbox"][0], i["bbox"][3] - i["bbox"][1])
                for page in doc for i in page.get_image_info()]
    original, preview = placed(reference / "main.pdf"), placed(res.pdf_path)
    assert [o[0] for o in original] == [9000, 9000] and [p[0] for p in preview] == [2250, 2250]
    for (_, ow, oh), (_, pw, ph) in zip(original, preview):
        assert abs(pw - ow) / ow < 0.002 and abs(ph - oh) / oh < 0.002, (original, preview)
    assert texmemory_size(repo / "big.png") == (9000, 5000)  # the project's own image is untouched


def texmemory_size(path: Path):
    from leafbridge import texmemory
    return texmemory.image_size(path)
