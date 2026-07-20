"""Lightweight project statistics: word counts, TODO markers, undefined refs.

Pure text analysis over the checked-out repo (no compile, no Perl). Word counts
are approximate (comments, commands, math, and environments are stripped) but
consistent, which is what trimming toward a journal limit needs.
"""

from __future__ import annotations

import re
from pathlib import Path

_COMMENT = re.compile(r"(?<!\\)%.*")
_ENV_DROP = re.compile(
    r"\\begin\{(equation|align|figure|table|tikzpicture|thebibliography|verbatim|lstlisting)\*?\}"
    r".*?\\end\{\1\*?\}", re.S)
_CMD = re.compile(r"\\[a-zA-Z@]+(\[[^\]]*\])?(\{[^{}]*\})?")
_MATH = re.compile(r"\$[^$]*\$")
_TODO = re.compile(r"(TODO|FIXME|XXX|\\todo\b)", re.I)
_LABEL = re.compile(r"\\label\{([^}]+)\}")
_REF = re.compile(r"\\(?:ref|eqref|autoref|cref|Cref|pageref)\{([^}]+)\}")


def word_count(tex_text: str) -> int:
    t = _COMMENT.sub("", tex_text)
    t = _ENV_DROP.sub(" ", t)
    t = _MATH.sub(" EQN ", t)
    t = _CMD.sub(" ", t)
    t = re.sub(r"[{}\\]", " ", t)
    return len([w for w in t.split() if any(c.isalnum() for c in w)])


def analyze(repo: Path) -> dict:
    """Per-file word counts + TODO markers + undefined/unused \\ref labels."""
    counts: dict[str, int] = {}
    todos: list[str] = []
    labels: set[str] = set()
    refs: set[str] = set()
    for p in sorted(repo.rglob("*.tex")):
        if ".git" in p.parts:
            continue
        rel = p.relative_to(repo).as_posix()
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        counts[rel] = word_count(text)
        for i, line in enumerate(text.splitlines(), 1):
            if _TODO.search(_COMMENT.sub(lambda m: m.group(0), line)):
                todos.append(f"{rel}:{i}: {line.strip()[:100]}")
        labels.update(_LABEL.findall(text))
        for grp in _REF.findall(text):
            refs.update(r.strip() for r in grp.split(","))
    return {
        "counts": counts,
        "total": sum(counts.values()),
        "todos": todos[:40],
        "undefined_refs": sorted(refs - labels),
        "unused_labels": sorted(labels - refs),
    }
