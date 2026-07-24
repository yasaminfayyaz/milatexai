"""Tests for the /tools/bibtex cleaner: the client-side engine (run in a real JS
engine via QuickJS) and the static page itself."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from leafbridge import tool_bibtex

_HTML = tool_bibtex.render_bibtex_tool()
_BATTERY = (Path(__file__).with_name("bibtex_battery.js")).read_text(encoding="utf-8")


def _engine_js() -> str:
    """The pure engine JS (everything before the DOM/UI section), with a stub
    ``window`` so ``window.BIB = {...}`` works headlessly."""
    m = re.search(r"<script>(.*?)</script>", _HTML, re.S)
    assert m, "no <script> block in the tool"
    js = m.group(1)
    ui = js.find("/* ===================== UI")
    assert ui > 0, "UI marker not found — engine/UI split changed"
    return "var window = {};\n" + js[:ui]


def test_engine_passes_full_battery():
    quickjs = pytest.importorskip("quickjs")
    ctx = quickjs.Context()
    ctx.eval(_engine_js())
    res = json.loads(ctx.eval(_BATTERY))
    assert res["fail"] == 0, res["failures"]
    assert res["pass"] >= 45, f"expected many assertions, ran {res['pass']}"


def test_page_is_self_contained_and_static():
    html = tool_bibtex.render_bibtex_tool()
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert "BibTeX Cleaner" in html
    # funnel to the product is present
    assert "milatexai.com" in html and "Add the connector" in html
    # CSP-safe / edge-cacheable: no external script or stylesheet assets
    assert re.search(r"<script[^>]+\bsrc=", html) is None, "external <script src> found"
    assert re.search(r"<link[^>]+stylesheet", html) is None, "external stylesheet found"
    # SEO basics
    assert "<title>" in html and '<meta name="description"' in html


def test_route_registered_and_edge_cached():
    # served by the hosted app, and edge-cached so it never wakes the container
    from leafbridge import asgi

    assert "/tools/bibtex" in asgi._SecurityHeaders._EDGE_CACHED
