"""Figure Studio conventions: where figure code lives and how it is tracked.

The user's Overleaf repo is the source of truth. Each managed figure is a pair

    figures/src/<slug>.py    the matplotlib source, with a machine-readable header
    figures/<slug>.pdf       the rendered artifact \\includegraphics uses

joined by one kebab-case *slug* (also the suggested ``\\label{fig:<slug>}``).
Deletion tracking is dynamic and index-free: the repo scan reports what exists
now, and git history (within the shallow-clone window) remembers scripts that
were deleted, so they can be recovered with ``git show``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SRC_DIR = "figures/src"
OUT_DIR = "figures"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")

_HEADER_KEY = re.compile(r"^#\s*(figure|output|created|tool)\s*:\s*(.+?)\s*$")


class FigureError(Exception):
    pass


def slugify(name: str) -> str:
    """Normalize a user-facing figure name to a slug; reject empties."""
    slug = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]+", "-", (name or "").strip().lower())).strip("-")
    slug = slug[:40]
    if not SLUG_RE.match(slug or ""):
        raise FigureError(
            "Figure names must contain letters or digits (e.g. 'energy-vs-time')."
        )
    return slug


def src_path(slug: str) -> str:
    return f"{SRC_DIR}/{slug}.py"


def out_path(slug: str) -> str:
    return f"{OUT_DIR}/{slug}.pdf"


def build_header(slug: str, *, created: str | None = None) -> str:
    created = created or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        "# === milatexai figure ===\n"
        f"# figure: {slug}\n"
        f"# output: {out_path(slug)}\n"
        f"# created: {created}\n"
        "# tool: milatexai/1\n"
        f"# Regenerate: run this file with matplotlib installed; it writes {out_path(slug)}\n"
        "# ========================\n"
    )


def parse_header(text: str) -> dict[str, str] | None:
    """Parse the header comment tolerantly; None if this isn't a managed figure.
    Only the ``figure:`` line is load-bearing."""
    fields: dict[str, str] = {}
    for line in text.splitlines()[:15]:
        m = _HEADER_KEY.match(line)
        if m:
            fields[m.group(1)] = m.group(2)
    return fields if "figure" in fields else None


@dataclass
class FigureInfo:
    slug: str
    src: str
    out: str
    out_exists: bool


def scan_figures(repo: Path) -> list[FigureInfo]:
    """The repo's managed figures, from convention + headers. Repo wins on
    existence; a file without a parseable header still counts by filename."""
    src_dir = repo / SRC_DIR
    if not src_dir.is_dir():
        return []
    found: list[FigureInfo] = []
    for p in sorted(src_dir.glob("*.py")):
        try:
            head = parse_header(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            head = None
        slug = (head or {}).get("figure", p.stem)
        declared_out = (head or {}).get("output", out_path(slug))
        found.append(FigureInfo(
            slug=slug, src=f"{SRC_DIR}/{p.name}", out=declared_out,
            out_exists=(repo / declared_out).is_file(),
        ))
    return found


def parse_deleted(git_log_output: str, live_slugs: set[str]) -> dict[str, str]:
    """From ``git log --diff-filter=D --format=@%h --name-only -- figures/src``
    output, map deleted-figure slug -> last commit that still knew it (the
    deleting commit; ``git show <hash>^:<path>`` recovers the code). Slugs that
    exist again now (re-added) are excluded."""
    deleted: dict[str, str] = {}
    current = ""
    for line in git_log_output.splitlines():
        line = line.strip()
        if line.startswith("@"):
            current = line[1:]
        elif line.startswith(f"{SRC_DIR}/") and line.endswith(".py"):
            slug = Path(line).stem
            if slug not in live_slugs and slug not in deleted:
                deleted[slug] = current
    return deleted


def pdf_to_png(pdf_bytes: bytes, dpi: int = 150) -> bytes:
    """First page of a PDF as PNG (the committed artifact's preview)."""
    import fitz  # PyMuPDF

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return doc[0].get_pixmap(dpi=dpi).tobytes("png")
    finally:
        doc.close()
