"""Remember compile results per exact version of a paper, so nothing compiles twice.

check_compile, show_page, show_table and show_figure used to compile the whole paper
every time, even when nothing had changed: a round of "does it compile, show me page 3,
show me table 2" meant four identical compiles of about 30 seconds each.

A result is stored under (project, commit id, root .tex file, CACHE_VERSION). The commit
id is the version the working copy is at; callers refresh the working copy from the
remote first (GitWorker.open_repo(fresh=True)), so an edit made anywhere, through
MiLatexAI or directly in Overleaf, GitHub or GitLab, is a new commit id and therefore a
new compile. A result can never be served for a version it was not built from.

Results live on this copy's own disk (a cache, not shared state): another copy simply
compiles once itself. Only the last few versions per project are kept, and the whole
cache disappears when a copy restarts.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

# Bump when the way results are produced changes (for example the float locator), so
# results built the old way are never reused.
CACHE_VERSION = "2"
KEEP_PER_PROJECT = 3


@dataclass
class Located:
    """The page-locating compile of one version: did it build, the PDF, where each float landed."""
    clean: bool
    pages: int | None = None
    floats: dict[str, list[int]] = field(default_factory=dict)        # "table:2" -> [start, end]
    labels: dict[str, list] = field(default_factory=dict)             # "tab:x" -> [number, page]
    pdf_path: str | None = None
    message: str = ""


@dataclass
class Plain:
    """The ordinary compile of one version: the authoritative verdict and the real errors."""
    available: bool
    ok: bool
    pages: int | None = None
    errors: list[str] = field(default_factory=list)
    message: str = ""


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:80] or "_"


class BuildCache:
    def __init__(self, root: Path, keep: int = KEEP_PER_PROJECT):
        self.root = Path(root)
        self.keep = keep

    def _dir(self, project_id: str, commit: str, main: str, kind: str) -> Path:
        key = hashlib.sha256(f"{CACHE_VERSION}|{commit}|{main}|{kind}".encode()).hexdigest()[:32]
        return self.root / _safe(project_id) / key

    async def located(self, project_id: str, commit: str, main: str,
                      build: Callable[[], Awaitable[Located]]) -> tuple[Located, bool]:
        """Return (result, came_from_cache)."""
        d = self._dir(project_id, commit, main, "located")
        hit = self._read(d)
        if hit is not None:
            res = Located(**hit)
            if not res.pdf_path or Path(res.pdf_path).is_file():
                return res, True
        res = await build()
        self._write(d, asdict(res), pdf=res.pdf_path)
        if res.pdf_path:
            res.pdf_path = str(d / "doc.pdf")
        return res, False

    async def plain(self, project_id: str, commit: str, main: str,
                    build: Callable[[], Awaitable[Plain]]) -> tuple[Plain, bool]:
        d = self._dir(project_id, commit, main, "plain")
        hit = self._read(d)
        if hit is not None:
            return Plain(**hit), True
        res = await build()
        if res.available:                 # never remember "engine unavailable"
            self._write(d, asdict(res))
        return res, False

    # -- storage ------------------------------------------------------------------

    @staticmethod
    def _read(d: Path) -> dict | None:
        try:
            return json.loads((d / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write(self, d: Path, data: dict, pdf: str | None = None) -> None:
        try:
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True, exist_ok=True)
            if pdf:
                shutil.copyfile(pdf, d / "doc.pdf")
                data = {**data, "pdf_path": str(d / "doc.pdf")}
            # result.json last: an entry without it is incomplete and never read.
            (d / "result.json").write_text(json.dumps(data), encoding="utf-8")
            self._evict(d.parent)
        except OSError:
            shutil.rmtree(d, ignore_errors=True)   # a cache that cannot write just doesn't remember

    def _evict(self, project_dir: Path) -> None:
        entries = sorted((p for p in project_dir.iterdir() if p.is_dir()),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        for old in entries[self.keep * 2:]:        # "located" and "plain" per version
            shutil.rmtree(old, ignore_errors=True)


# -- conversions to and from what the compile helpers return -----------------------------

def from_locate(res) -> Located:
    """texlocate.LocateResult -> Located (JSON friendly)."""
    floats = {f"{k}:{n}": [f.start_page, f.end_page] for (k, n), f in res.floats.items()}
    labels = {name: [num, page] for name, (num, page) in res.labels.items()}
    return Located(clean=bool(getattr(res, "clean", False)), pages=getattr(res, "pages", None), floats=floats,
                   labels=labels, pdf_path=res.pdf_path, message=res.message)


def to_locate(loc: Located):
    """Located -> texlocate.LocateResult, for resolve_number / float_listing / rendering."""
    from .texlocate import Float, LocateResult

    floats = {}
    for key, (start, end) in loc.floats.items():
        kind, num = key.split(":", 1)
        floats[(kind, int(num))] = Float(kind=kind, number=int(num), start_page=start, end_page=end)
    labels = {name: (num, page) for name, (num, page) in loc.labels.items()}
    return LocateResult(ok=bool(floats), floats=floats, labels=labels, pdf_path=loc.pdf_path,
                        message=loc.message, clean=loc.clean, pages=loc.pages)


def from_compile(res) -> Plain:
    """texcompile.CompileResult -> Plain."""
    return Plain(available=res.available, ok=res.ok, pages=res.pages, errors=list(res.errors), message=res.message)
