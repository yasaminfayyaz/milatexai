"""Unit tests for the pure (no-network) logic: LaTeX parsing, project-id
extraction, and path-traversal safety."""

from __future__ import annotations

import pytest

from leafbridge import latex
from leafbridge.config import ConfigError, extract_project_id
from leafbridge.files import (
    PathError,
    number_lines,
    safe_join,
    search_files,
    write_text_exact,
)

HEX = "0123456789abcdef01234567"  # 24-char ObjectId

SAMPLE = r"""\documentclass{article}
\begin{document}
\section{Introduction}
Intro text here.
% \section{This is commented out and must be ignored}
\subsection{Background}
some background
\section{Methods}
\subsection{A \textbf{bold} step}
do the thing
\end{document}
"""


# --- project id extraction -------------------------------------------------

@pytest.mark.parametrize(
    "value",
    [
        HEX,
        f"https://www.overleaf.com/project/{HEX}",
        f"https://www.overleaf.com/project/{HEX}?some=query",
        f"https://git.overleaf.com/{HEX}",
        f"  https://www.overleaf.com/project/{HEX}/  ",
    ],
)
def test_extract_project_id_ok(value):
    assert extract_project_id(value) == HEX


def test_extract_project_id_rejects_share_link():
    # A 16-hex read/write share token is not a project id.
    with pytest.raises(ConfigError):
        extract_project_id("https://www.overleaf.com/1234567890abcdef")


def test_extract_project_id_rejects_garbage():
    with pytest.raises(ConfigError):
        extract_project_id("not a url at all")


# --- LaTeX section parsing -------------------------------------------------

def test_find_sections_titles_and_order():
    secs = latex.find_sections(SAMPLE)
    titles = [(s.kind, s.title) for s in secs]
    assert titles == [
        ("section", "Introduction"),
        ("subsection", "Background"),
        ("section", "Methods"),
        ("subsection", "A bold step"),  # \textbf stripped, braces gone
    ]


def test_commented_section_ignored():
    secs = latex.find_sections(SAMPLE)
    assert all("commented out" not in s.title for s in secs)


def test_section_line_ranges_nest_correctly():
    secs = {s.title: s for s in latex.find_sections(SAMPLE)}
    intro = secs["Introduction"]
    methods = secs["Methods"]
    # Introduction spans until just before Methods (a same-level section),
    # i.e. it contains its Background subsection.
    assert intro.line < secs["Background"].line < methods.line
    assert intro.end_line == methods.line - 1


def test_find_section_partial_match():
    found = latex.find_section(SAMPLE, "background")
    assert found is not None
    section, body = found
    assert section.kind == "subsection"
    assert "some background" in body


def test_find_section_missing():
    assert latex.find_section(SAMPLE, "nonexistent") is None


# --- path safety -----------------------------------------------------------

def test_safe_join_ok(tmp_path):
    (tmp_path / "sections").mkdir()
    p = safe_join(tmp_path, "sections/intro.tex")
    assert str(p).startswith(str(tmp_path.resolve()))


def test_safe_join_blocks_traversal(tmp_path):
    with pytest.raises(PathError):
        safe_join(tmp_path, "../secret.txt")


def test_safe_join_blocks_git_dir(tmp_path):
    with pytest.raises(PathError):
        safe_join(tmp_path, ".git/config")


def test_safe_join_blocks_empty(tmp_path):
    with pytest.raises(PathError):
        safe_join(tmp_path, "   ")


# --- misc helpers ----------------------------------------------------------

def test_number_lines():
    out = number_lines("a\nb\nc")
    assert out.splitlines()[0].endswith("\ta")
    assert "3\tc" in out


def test_write_text_exact_preserves_newlines(tmp_path):
    # Regression: Path.write_text on Windows translates \n -> \r\n, which turns
    # existing \r\n into \r\r\n and corrupts a whole file's line endings. The
    # exact writer must store bytes verbatim.
    p = tmp_path / "f.tex"

    write_text_exact(p, "a\nb\nc\n")
    assert p.read_bytes() == b"a\nb\nc\n"  # LF stays LF, no CR added

    write_text_exact(p, "x\r\ny\r\n")
    assert p.read_bytes() == b"x\r\ny\r\n"  # CRLF preserved, NOT doubled to \r\r\n
    assert b"\r\r" not in p.read_bytes()


def test_search_files(tmp_path):
    (tmp_path / "a.tex").write_text("hello world\nfoo BAR baz\n", encoding="utf-8")
    (tmp_path / "b.bib").write_text("no match here\n", encoding="utf-8")
    hits = search_files(tmp_path, "bar")
    assert len(hits) == 1
    assert hits[0].path == "a.tex"
    assert hits[0].line == 2
