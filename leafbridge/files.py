"""Safe filesystem helpers scoped to a single project clone.

Every path a tool touches goes through :func:`safe_join`, which guarantees the
result stays inside the project directory and never reaches into ``.git``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Files we list by default. Reads/writes are allowed on any text file inside the
# project, but listings focus on LaTeX source so results stay useful.
SOURCE_EXTENSIONS = {
    ".tex", ".bib", ".cls", ".sty", ".bst", ".tikz", ".rnw",
    ".ins", ".dtx", ".def", ".clo", ".lco", ".md", ".txt",
}

# Never descend into these directory names.
_SKIP_DIRS = {".git", ".github", "__pycache__"}

# Refuse to dump anything larger than this from read_file (bytes).
MAX_READ_BYTES = 2 * 1024 * 1024


class PathError(Exception):
    """A requested path was unsafe or invalid."""


def safe_join(repo_root: Path, rel: str) -> Path:
    """Resolve ``rel`` against ``repo_root`` and refuse anything that escapes it.

    Rejects absolute paths, ``..`` traversal, and anything under ``.git``.
    """
    if rel is None or str(rel).strip() == "":
        raise PathError("Empty file path.")
    rel_norm = str(rel).strip().replace("\\", "/").lstrip("/")
    if not rel_norm:
        raise PathError("File path resolves to the project root, not a file.")

    root = repo_root.resolve()
    target = (root / rel_norm).resolve()

    try:
        rel_check = target.relative_to(root)
    except ValueError:
        raise PathError(f"Path {rel!r} is outside the project directory.") from None

    parts = rel_check.parts
    if parts and parts[0] in _SKIP_DIRS:
        raise PathError(f"Path {rel!r} points inside a protected directory.")
    return target


def to_rel(repo_root: Path, path: Path) -> str:
    return path.resolve().relative_to(repo_root.resolve()).as_posix()


@dataclass
class FileEntry:
    path: str
    size: int
    ext: str


def list_source_files(repo_root: Path, all_files: bool = False) -> list[FileEntry]:
    """List project files (LaTeX source by default; every file if ``all_files``)."""
    root = repo_root.resolve()
    entries: list[FileEntry] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            ext = Path(fn).suffix.lower()
            if not all_files and ext not in SOURCE_EXTENSIONS:
                continue
            full = Path(dirpath) / fn
            try:
                size = full.stat().st_size
            except OSError:
                size = 0
            entries.append(FileEntry(path=to_rel(root, full), size=size, ext=ext))
    entries.sort(key=lambda e: e.path.lower())
    return entries


def read_text(path: Path) -> str:
    """Read a text file, raising PathError for missing/binary/oversized files."""
    if not path.exists():
        raise PathError(f"File not found: does not exist in the project.")
    if not path.is_file():
        raise PathError("Path is a directory, not a file.")
    size = path.stat().st_size
    if size > MAX_READ_BYTES:
        raise PathError(
            f"File is too large to read ({size} bytes; limit {MAX_READ_BYTES})."
        )
    data = path.read_bytes()
    if b"\x00" in data:
        raise PathError("File appears to be binary; refusing to read as text.")
    return data.decode("utf-8", errors="replace")


def number_lines(text: str, start: int = 1) -> str:
    """Render text with right-aligned line numbers (like ``cat -n``)."""
    lines = text.splitlines()
    if not lines:
        return "(empty file)"
    width = len(str(start + len(lines) - 1))
    return "\n".join(
        f"{str(i).rjust(width)}\t{line}" for i, line in enumerate(lines, start=start)
    )


@dataclass
class SearchHit:
    path: str
    line: int
    text: str


def search_files(
    repo_root: Path, query: str, *, max_hits: int = 100, all_files: bool = False
) -> list[SearchHit]:
    """Case-insensitive substring search across project source files."""
    q = query.strip().lower()
    if not q:
        return []
    hits: list[SearchHit] = []
    for entry in list_source_files(repo_root, all_files=all_files):
        path = safe_join(repo_root, entry.path)
        try:
            content = read_text(path)
        except PathError:
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            if q in line.lower():
                hits.append(SearchHit(entry.path, lineno, line.strip()[:300]))
                if len(hits) >= max_hits:
                    return hits
    return hits
