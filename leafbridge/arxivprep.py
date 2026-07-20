"""arXiv submission prep: flatten, gather the .bbl, bundle a submission zip.

The chronic Overleaf->arXiv pains this removes: arXiv will not run bibtex (it
needs the compiled .bbl, which Overleaf's zip omits), multi-file projects
confuse its processor, and stray comments/junk trip moderation. We flatten all
\\input/\\include into one .tex, drop comment-only lines, compile once to
harvest the .bbl, and zip exactly what arXiv needs.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

_INPUT = re.compile(r"\\(?:input|include)\{([^}]+)\}")
_GRAPHIC = re.compile(r"\\includegraphics\s*(?:\[[^\]]*\])?\{([^}]+)\}")
_BIBLIO = re.compile(r"\\bibliography\{[^}]+\}")
_GRAPH_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".eps")


class ArxivPrepError(Exception):
    pass


async def compile_bbl(repo: Path, main_rel: str, timeout: int = 240) -> str | None:
    """Compile once keeping intermediates; return the .bbl text if one exists."""
    import asyncio
    import subprocess
    import tempfile

    from . import texcompile

    exe = texcompile.tectonic_path()
    if not exe:
        return None

    def _run() -> str | None:
        with tempfile.TemporaryDirectory(prefix="mila_bbl_") as outdir:
            try:
                subprocess.run(
                    [exe, "-X", "compile", "--keep-intermediates", "--outdir", outdir, main_rel],
                    cwd=str(repo), capture_output=True, text=True, timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                return None
            bbl = Path(outdir) / (Path(main_rel).stem + ".bbl")
            return bbl.read_text(encoding="utf-8", errors="replace") if bbl.is_file() else None

    return await asyncio.to_thread(_run)


def flatten(repo: Path, main_rel: str, _seen: set[str] | None = None) -> str:
    """Inline every \\input/\\include recursively; drop comment-only lines."""
    seen = _seen if _seen is not None else set()
    if main_rel in seen:
        return f"% (skipped circular include of {main_rel})\n"
    seen.add(main_rel)
    path = repo / main_rel
    if not path.is_file() and not main_rel.endswith(".tex"):
        path = repo / (main_rel + ".tex")
    if not path.is_file():
        return f"% (missing file: {main_rel})\n"
    out: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True):
        if line.lstrip().startswith("%"):
            continue  # comment-only line

        def _inline(m: re.Match) -> str:
            rel = m.group(1).strip()
            rel = rel if rel.endswith(".tex") else rel + ".tex"
            return "\n" + flatten(repo, rel, seen) + "\n"

        out.append(_INPUT.sub(_inline, line))
    return "".join(out)


def referenced_graphics(flat_tex: str, repo: Path) -> list[str]:
    """Repo-relative graphics files the flattened doc references (resolved)."""
    found: list[str] = []
    for raw in _GRAPHIC.findall(flat_tex):
        raw = raw.strip()
        candidates = [raw] if raw.lower().endswith(_GRAPH_EXTS) else [
            raw + e for e in _GRAPH_EXTS
        ]
        for c in candidates:
            if (repo / c).is_file():
                found.append(c)
                break
    return sorted(set(found))


def build_zip(
    repo: Path, main_rel: str, bbl: str | None
) -> tuple[bytes, list[str]]:
    """The submission zip: flattened main.tex (+ inlined .bbl or a note),
    graphics, and any custom .cls/.bst/.sty at the repo root."""
    flat = flatten(repo, main_rel)
    manifest: list[str] = []
    if bbl:
        # arXiv-recommended: name the .bbl after the main file; keep \bibliography
        # replaced so arXiv never tries to run bibtex.
        flat = _BIBLIO.sub(r"\\input{main.bbl}", flat)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("main.tex", flat)
        manifest.append("main.tex (flattened)")
        if bbl:
            z.writestr("main.bbl", bbl)
            manifest.append("main.bbl (precompiled bibliography)")
        for g in referenced_graphics(flat, repo):
            z.write(repo / g, g)
            manifest.append(g)
        for p in sorted(repo.glob("*")):
            if p.suffix in (".cls", ".bst", ".sty") and p.is_file():
                z.write(p, p.name)
                manifest.append(p.name)
    return buf.getvalue(), manifest
