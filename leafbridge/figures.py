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

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SRC_DIR = "figures/src"
OUT_DIR = "figures"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")

_HEADER_KEY = re.compile(r"^[#%]\s*(figure|output|created|tool|code-sha256|output-sha256)\s*:\s*(.+?)\s*$")
_HEADER_END_BODY = "========================"


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


def src_path(slug: str, src_ext: str = "py") -> str:
    return f"{SRC_DIR}/{slug}.{src_ext}"


def out_path(slug: str, ext: str = "pdf") -> str:
    return f"{OUT_DIR}/{slug}.{ext}"


def build_header(
    slug: str, *, code_body: str = "", pdf_bytes: bytes = b"",
    created: str | None = None, ext: str = "pdf", comment: str = "#",
) -> str:
    """Header written above the code. The two sha256 lines are the provenance
    record: they prove later whether the code body and the committed PDF are
    still the pair this tool produced, or were changed outside Figure Studio."""
    created = created or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    c = comment
    regen = ("run this file with matplotlib installed" if c == "#"
             else "compile this TikZ source standalone")
    return (
        f"{c} === milatexai figure ===\n"
        f"{c} figure: {slug}\n"
        f"{c} output: {out_path(slug, ext)}\n"
        f"{c} created: {created}\n"
        f"{c} tool: milatexai/1\n"
        f"{c} code-sha256: {hashlib.sha256(code_body.encode('utf-8')).hexdigest()}\n"
        f"{c} output-sha256: {hashlib.sha256(pdf_bytes).hexdigest()}\n"
        f"{c} Regenerate: {regen}; it produces {out_path(slug, ext)}\n"
        f"{c} {_HEADER_END_BODY}\n"
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
    for p in sorted(list(src_dir.glob("*.py")) + list(src_dir.glob("*.tex"))):
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


def split_body(src_text: str) -> str | None:
    """The code below the header, or None if there is no header terminator."""
    lines = src_text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        s = line.rstrip()
        if s in (f"# {_HEADER_END_BODY}", f"% {_HEADER_END_BODY}"):
            return "".join(lines[i + 1:])
    return None


# Sync states: is the stored code still ground truth for the committed artifact?
IN_SYNC = "in-sync"                    # code and PDF are the pair we committed
CODE_EDITED = "code-edited"            # .py changed since last render; PDF stale
ARTIFACT_REPLACED = "artifact-replaced"  # PDF changed OUTSIDE Figure Studio
DIVERGED = "diverged"                  # both changed independently
OUTPUT_MISSING = "output-missing"
UNTRACKED = "untracked"                # no provenance hashes (older / hand-made)


def sync_state(repo: Path, info: FigureInfo) -> str:
    """Compare the header's provenance hashes against what is on disk NOW."""
    try:
        src_text = (repo / info.src).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return UNTRACKED
    head = parse_header(src_text) or {}
    body = split_body(src_text)
    if body is None or "code-sha256" not in head or "output-sha256" not in head:
        return UNTRACKED
    code_ok = hashlib.sha256(body.encode("utf-8")).hexdigest() == head["code-sha256"]
    out_file = repo / info.out
    if not out_file.is_file():
        return OUTPUT_MISSING
    out_ok = hashlib.sha256(out_file.read_bytes()).hexdigest() == head["output-sha256"]
    if code_ok and out_ok:
        return IN_SYNC
    if out_ok:
        return CODE_EDITED
    if code_ok:
        return ARTIFACT_REPLACED
    return DIVERGED


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
