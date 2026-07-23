"""_resolve_main_tex: pick a specific .tex (multi-document repos) or auto-detect."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from leafbridge.hosted import _resolve_main_tex

DOC = "\\documentclass{article}\\begin{document}%s\\end{document}\n"


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "a.tex").write_text(DOC % "A")
    (tmp_path / "papers").mkdir()
    (tmp_path / "papers" / "b.tex").write_text(DOC % "B")
    return tmp_path


def test_explicit_tex_targets_that_file(tmp_path):
    repo = _repo(tmp_path)
    assert _resolve_main_tex(repo, "papers/b.tex") == "papers/b.tex"


def test_auto_detect_when_omitted(tmp_path):
    repo = _repo(tmp_path)
    assert _resolve_main_tex(repo, None) in ("a.tex", "papers/b.tex")


def test_unknown_tex_lists_candidates(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(ToolError) as e:
        _resolve_main_tex(repo, "nope.tex")
    msg = str(e.value)
    assert "a.tex" in msg and "papers/b.tex" in msg


def test_non_tex_path_rejected(tmp_path):
    repo = _repo(tmp_path)
    (repo / "readme.md").write_text("hi")
    with pytest.raises(ToolError):
        _resolve_main_tex(repo, "readme.md")


def test_traversal_blocked(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(Exception):  # safe_join raises PathError
        _resolve_main_tex(repo, "../../etc/passwd")


def test_no_root_and_no_tex_arg_raises(tmp_path):
    with pytest.raises(ToolError):
        _resolve_main_tex(tmp_path, None)  # empty repo, no .tex
