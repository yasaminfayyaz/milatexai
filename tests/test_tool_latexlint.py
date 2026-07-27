"""Tests for the /tools/latex-error-finder page: the client-side linter engine
(run in QuickJS) and the static page itself."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from leafbridge import tool_latexlint

_HTML = tool_latexlint.render_latexlint_tool()
_BATTERY = (Path(__file__).with_name("latexlint_battery.js")).read_text(encoding="utf-8")


def _engine_js() -> str:
    m = re.search(r"<script>(.*?)</script>", _HTML, re.S)
    assert m, "no <script> block in the tool"
    js = m.group(1)
    ui = js.find("/* ===================== UI")
    assert ui > 0, "UI marker not found: engine/UI split changed"
    return "var window = {};\n" + js[:ui]


def _ctx():
    quickjs = pytest.importorskip("quickjs")
    ctx = quickjs.Context()
    ctx.eval(_engine_js())
    return ctx


def _lint(ctx, src: str) -> dict:
    return json.loads(ctx.eval("JSON.stringify(window.LATEXLINT.lint(" + json.dumps(src) + "))"))


def test_engine_passes_full_battery():
    ctx = _ctx()
    res = json.loads(ctx.eval(_BATTERY))
    assert res["fail"] == 0, res["failures"]
    assert res["pass"] >= 90, f"expected many assertions, ran {res['pass']}"


def test_advisory_warnings_and_safe_negatives():
    """Text-mode _ ^ & are flagged as warnings, but never inside math, labels,
    citations, URLs, or file names (the fail-proof guarantee)."""
    ctx = _ctx()
    assert _lint(ctx, "the value x_2 is")["warnings"] == 1
    assert _lint(ctx, "E = mc^2 in text")["warnings"] == 1
    assert _lint(ctx, "Tom & Jerry")["warnings"] == 1
    for safe in ("$x_2$", "\\[ y^3 \\]", "\\label{fig:a_b}", "\\cite{smith_2020}",
                 "\\url{http://a.com/x_y?p=1&q=2}", "\\includegraphics{plot_1}",
                 "\\begin{align} a &= b_i^j \\end{align}",
                 "\\begin{tabular}{cc} a & b \\end{tabular}"):
        assert _lint(ctx, safe)["ok"], safe


def test_realistic_document_is_clean():
    """A valid multi-feature document must produce zero false positives."""
    quickjs = pytest.importorskip("quickjs")
    ctx = quickjs.Context()
    ctx.eval(_engine_js())
    doc = (
        "\\documentclass[11pt]{article}\n"
        "\\usepackage{amsmath,booktabs}\n"
        "\\newcommand{\\R}{\\mathbb{R}}\n"
        "\\begin{document}\n"
        "Cost is \\$5 and 50\\% done. Math $x^2+y^2$ and \\[ \\left(\\frac{a}{b}\\right). \\]\n"
        "\\begin{itemize}\\item a \\& b\\end{itemize}\n"
        "\\begin{lstlisting}\nif x: { y\n\\end{lstlisting}\n"
        "\\verb|{unbalanced|\n"
        "\\end{document}\n"
        "trailing { junk ignored\n"
    )
    res = json.loads(ctx.eval("JSON.stringify(window.LATEXLINT.lint(" + json.dumps(doc) + "))"))
    assert res["ok"] is True, res["issues"]


def test_page_is_self_contained_and_static():
    html = tool_latexlint.render_latexlint_tool()
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert "LaTeX Error Finder" in html
    assert "milatexai.com" in html and "Add the connector" in html
    assert re.search(r"<script[^>]+\bsrc=", html) is None, "external <script src> found"
    assert re.search(r"<link[^>]+stylesheet", html) is None, "external stylesheet found"
    assert "<title>" in html and '<meta name="description"' in html


def test_route_registered_and_edge_cached():
    from leafbridge import asgi

    assert "/tools/latex-error-finder" in asgi._SecurityHeaders._EDGE_CACHED
