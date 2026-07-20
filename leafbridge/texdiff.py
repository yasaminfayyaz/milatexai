"""Tracked-changes PDF: latexdiff between an old revision and the working tree.

Runs latexdiff (vendored perl script) on the project's main .tex, compiles the
marked-up result with Tectonic *inside the repo checkout* (so \\input files,
figures, and classes resolve), and returns the PDF. Temp files are cleaned even
on failure so a later commit can never pick them up.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class TexDiffError(Exception):
    pass


def latexdiff_cmd() -> list[str] | None:
    """How to invoke latexdiff, or None if unavailable."""
    override = os.environ.get("LEAFBRIDGE_LATEXDIFF")
    if override and Path(override).exists():
        return ["perl", override]
    exe = shutil.which("latexdiff")
    return [exe] if exe else None


async def diff_pdf(repo: Path, main_rel: str, old_text: str, timeout: int = 240) -> bytes:
    """PDF with old->current changes marked up. Caller holds the repo lock."""
    cmd = latexdiff_cmd()
    if cmd is None:
        raise TexDiffError("latexdiff is unavailable on this server.")

    def _run() -> bytes:
        main = repo / main_rel
        old_f = repo / "__mila_old.tex"
        diff_f = repo / "__mila_diff.tex"
        try:
            old_f.write_text(old_text, encoding="utf-8")
            proc = subprocess.run(
                [*cmd, "--append-context2cmd=abstract", old_f.name, str(main.relative_to(repo).as_posix())],
                cwd=str(repo), capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                raise TexDiffError(
                    "latexdiff failed: " + (proc.stderr or "no output")[-400:])
            diff_f.write_text(proc.stdout, encoding="utf-8")
            with tempfile.TemporaryDirectory(prefix="mila_diff_") as outdir:
                cp = subprocess.run(
                    ["tectonic", "-X", "compile", "--outdir", outdir, diff_f.name]
                    if shutil.which("tectonic") else
                    [os.environ.get("LEAFBRIDGE_TECTONIC", "tectonic"), "-X", "compile",
                     "--outdir", outdir, diff_f.name],
                    cwd=str(repo), capture_output=True, text=True, timeout=timeout,
                )
                pdf = Path(outdir) / "__mila_diff.pdf"
                if cp.returncode != 0 or not pdf.is_file():
                    log = (cp.stderr or "") + (cp.stdout or "")
                    errs = [l.strip() for l in log.splitlines()
                            if l.strip().lower().startswith(("error", "!"))][:6]
                    raise TexDiffError(
                        "The marked-up document failed to compile (latexdiff markup can "
                        "clash with complex macros):\n" + ("\n".join(errs) or log[-500:]))
                return pdf.read_bytes()
        finally:
            for f in (old_f, diff_f):
                try:
                    f.unlink()
                except OSError:
                    pass

    return await asyncio.to_thread(_run)


def pdf_pages_to_pngs(pdf_bytes: bytes, max_pages: int = 8, dpi: int = 130) -> list[bytes]:
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return [doc[i].get_pixmap(dpi=dpi).tobytes("png")
                for i in range(min(doc.page_count, max_pages))]
    finally:
        doc.close()
