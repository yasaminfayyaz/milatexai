"""Guard: no em dashes (U+2014) or en dashes (U+2013) in shipped source.

House style forbids them in code, docstrings, UI copy, and docs. This scans the
package, tests, docs, and the README so a stray dash fails CI instead of shipping.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EM = chr(0x2014)  # em dash
EN = chr(0x2013)  # en dash
SCAN_EXTS = {".py", ".html", ".js", ".md", ".txt"}
SCAN_ROOTS = [ROOT / "leafbridge", ROOT / "tests", ROOT / "docs", ROOT / "README.md"]


def _files():
    for root in SCAN_ROOTS:
        if root.is_file():
            yield root
        elif root.is_dir():
            for p in root.rglob("*"):
                if p.is_file() and p.suffix in SCAN_EXTS and "__pycache__" not in p.parts:
                    yield p


def test_no_em_or_en_dashes():
    offenders = []
    for p in _files():
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if EM in line or EN in line:
                offenders.append(f"{p.relative_to(ROOT)}:{lineno}: {line.strip()[:90]}")
    assert not offenders, "em/en dashes found (use comma, colon, period, or parentheses):\n" + "\n".join(offenders)
