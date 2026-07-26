"""The free client-side BibTeX cleaner served at ``/tools/bibtex``.

Fully static, self-contained HTML + JS (no external assets, no server compute):
Cloudflare serves it from the edge, so it never wakes the container, a $0
faceless marketing surface that funnels to the MiLatexAI connector. The markup
lives in ``tool_bibtex.html`` beside this module (same pattern as
``site_content.json``), so its JS can be unit-tested directly.
"""

from __future__ import annotations

from pathlib import Path

_HTML = Path(__file__).with_name("tool_bibtex.html").read_text(encoding="utf-8")


def render_bibtex_tool() -> str:
    """The complete, static BibTeX-cleaner page."""
    return _HTML
