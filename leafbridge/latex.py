"""Lightweight LaTeX structure parsing for LeafBridge.

Just enough to answer "what sections are in this file?" and "give me the
`Introduction` section" without a full LaTeX parser. We deliberately keep this
regex/scan based: it must never crash on messy real-world .tex, only degrade to
finding fewer sections.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Sectioning commands, ordered from outermost to innermost. Lower level number
# means higher in the hierarchy (a \section ends at the next \section OR the
# next \chapter/\part, but not at a \subsection).
SECTION_LEVELS: dict[str, int] = {
    "part": -1,
    "chapter": 0,
    "section": 1,
    "subsection": 2,
    "subsubsection": 3,
    "paragraph": 4,
    "subparagraph": 5,
}

# The trailing (?![A-Za-z]) is a LaTeX control-word boundary: it stops
# \partial / \sectionmark / \subsectionfoo from being read as \part / \section /
# \subsection. \section, \section*, and \subparagraph{ still match.
_CMD_RE = re.compile(
    r"\\(" + "|".join(SECTION_LEVELS) + r")(\*)?(?![A-Za-z])",
)

# Sectioning commands inside these environments are code samples, not real
# sections, and must be ignored.
_VERBATIM_ENVS = {
    "verbatim", "verbatim*", "lstlisting", "minted", "Verbatim", "comment", "alltt",
}
_BEGIN_RE = re.compile(r"\\begin\{([^}]*)\}")


@dataclass(frozen=True)
class Section:
    """One sectioning unit and the line range it spans (all 1-based)."""

    kind: str  # "section", "subsection", ...
    level: int  # from SECTION_LEVELS
    title: str
    starred: bool
    line: int  # line the sectioning command sits on
    end_line: int  # last line belonging to this section (inclusive)

    @property
    def command(self) -> str:
        return "\\" + self.kind + ("*" if self.starred else "")


def strip_comment(line: str) -> str:
    """Return the code portion of a line, dropping a trailing LaTeX comment.

    A ``%`` starts a comment unless escaped as ``\\%``. Everything from the
    first unescaped ``%`` onward is removed.
    """
    out = []
    escaped = False
    for ch in line:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == "%":
            break
        out.append(ch)
    return "".join(out)


def _extract_braced(text: str, open_idx: int) -> tuple[str, int] | None:
    """Extract balanced ``{...}`` content starting at ``text[open_idx] == '{'``.

    Returns ``(content, index_after_closing_brace)`` or ``None`` if unbalanced.
    Respects backslash escaping of braces (``\\{`` / ``\\}``).
    """
    if open_idx >= len(text) or text[open_idx] != "{":
        return None
    depth = 0
    i = open_idx
    escaped = False
    while i < len(text):
        ch = text[i]
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i], i + 1
        i += 1
    return None


def find_sections(text: str) -> list[Section]:
    """Find all sectioning units in ``text`` with their line ranges.

    Commented-out section commands are ignored. Section titles may contain
    nested braces (e.g. ``\\section{A \\textbf{bold} title}``). Titles that span
    multiple lines are also handled.
    """
    lines = text.splitlines()
    # Comment-stripped view of each line, for detecting real commands.
    code_lines = [strip_comment(ln) for ln in lines]

    headers: list[tuple[int, str, bool, str]] = []  # (line_idx, kind, starred, title)
    in_verbatim: str | None = None

    for idx, code in enumerate(code_lines):
        if in_verbatim is not None:
            # Inside a code listing — ignore everything until it closes.
            if f"\\end{{{in_verbatim}}}" in lines[idx]:
                in_verbatim = None
            continue
        # If a verbatim-like environment opens on this line, scan only the code
        # before it and suppress detection until it closes (unless it's a one-liner).
        _bm = _BEGIN_RE.search(code)
        _opened = _bm.group(1) if (_bm and _bm.group(1) in _VERBATIM_ENVS) else None
        if _opened is not None and f"\\end{{{_opened}}}" not in lines[idx]:
            in_verbatim = _opened
        scan = code[: _bm.start()] if _opened is not None else code
        for m in _CMD_RE.finditer(scan):
            kind = m.group(1)
            starred = m.group(2) == "*"
            pos = m.end()
            # Skip an optional short-title argument: [ ... ]
            while pos < len(code) and code[pos].isspace():
                pos += 1
            if pos < len(code) and code[pos] == "[":
                depth = 0
                while pos < len(code):
                    if code[pos] == "[":
                        depth += 1
                    elif code[pos] == "]":
                        depth -= 1
                        if depth == 0:
                            pos += 1
                            break
                    pos += 1
            while pos < len(code) and code[pos].isspace():
                pos += 1
            if pos >= len(code) or code[pos] != "{":
                # No brace title on this line — could be a multi-line title;
                # join a few following code lines and retry once.
                joined = code[pos:] + "".join(
                    " " + code_lines[j] for j in range(idx + 1, min(idx + 4, len(code_lines)))
                )
                bpos = joined.find("{")
                if bpos == -1:
                    continue
                braced = _extract_braced(joined, bpos)
                title = braced[0] if braced else ""
            else:
                braced = _extract_braced(code, pos)
                title = braced[0] if braced else ""
            headers.append((idx, kind, starred, _clean_title(title)))

    sections: list[Section] = []
    total = len(lines)
    for h_i, (line_idx, kind, starred, title) in enumerate(headers):
        level = SECTION_LEVELS[kind]
        # End = one line before the next header at the same or higher level.
        end_line = total
        for j in range(h_i + 1, len(headers)):
            nxt_idx, nxt_kind, _, _ = headers[j]
            if SECTION_LEVELS[nxt_kind] <= level:
                end_line = nxt_idx  # 0-based index == line above it (1-based)
                break
        sections.append(
            Section(
                kind=kind,
                level=level,
                title=title,
                starred=starred,
                line=line_idx + 1,
                end_line=max(end_line, line_idx + 1),
            )
        )
    return sections


def _clean_title(title: str) -> str:
    """Tidy a raw title for display/matching: collapse whitespace, drop a few
    common formatting commands but keep the words."""
    t = re.sub(r"\\(textbf|textit|emph|texttt|textsf|mathrm|text)\s*", "", title)
    t = t.replace("{", "").replace("}", "")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def outline(sections: list[Section]) -> str:
    """Render an indented, line-numbered outline of the sections."""
    if not sections:
        return "(no sections found)"
    rows = []
    for s in sections:
        indent = "  " * max(s.level, 0)
        star = "*" if s.starred else ""
        rows.append(f"{indent}- [{s.line}-{s.end_line}] \\{s.kind}{star}: {s.title}")
    return "\n".join(rows)


def find_section(text: str, title: str) -> tuple[Section, str] | None:
    """Locate a section whose title matches ``title`` and return it + content.

    Matching is case-insensitive: exact match wins, otherwise the first section
    whose title contains the query (or vice-versa). Content spans the section's
    full line range, header line included.
    """
    sections = find_sections(text)
    if not sections:
        return None
    q = title.strip().lower()

    exact = [s for s in sections if s.title.lower() == q]
    chosen = exact[0] if exact else None
    if chosen is None:
        partial = [s for s in sections if q in s.title.lower() or s.title.lower() in q]
        chosen = partial[0] if partial else None
    if chosen is None:
        return None

    lines = text.splitlines()
    body = "\n".join(lines[chosen.line - 1 : chosen.end_line])
    return chosen, body
