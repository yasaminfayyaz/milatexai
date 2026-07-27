"""The free client-side LaTeX error finder served at ``/tools/latex-error-finder``.

Fully static, self-contained HTML + JS (no external assets, no server compute):
Cloudflare serves it from the edge, so it never wakes the container, a $0
faceless marketing surface that funnels to the MiLatexAI connector. The markup
lives in ``tool_latexlint.html`` beside this module, so its JS can be
unit-tested directly.
"""

from __future__ import annotations

from pathlib import Path

_HTML = Path(__file__).with_name("tool_latexlint.html").read_text(encoding="utf-8")


def render_latexlint_tool() -> str:
    """The complete, static LaTeX error finder page."""
    return _HTML
