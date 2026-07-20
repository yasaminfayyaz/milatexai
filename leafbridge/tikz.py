"""TikZ Diagram Studio: compile a standalone TikZ snippet to a PDF.

The LLM writes the TikZ; we wrap it in a minimal ``standalone`` document and
compile it with the Tectonic engine already in the image, seconds of CPU. The
committed source keeps the user's snippet (headered, editable forever) exactly
like Figure Studio does for matplotlib.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path

from . import texcompile


class TikzError(Exception):
    pass


def wrap(code: str) -> str:
    """Make the snippet a compilable standalone document (idempotent)."""
    if "\\documentclass" in code:
        return code
    body = code if "\\begin{tikzpicture}" in code else (
        "\\begin{tikzpicture}\n" + code.rstrip("\n") + "\n\\end{tikzpicture}"
    )
    return (
        "\\documentclass[tikz,border=4pt]{standalone}\n"
        "\\usepackage{pgfplots}\\pgfplotsset{compat=1.18}\n"
        "\\begin{document}\n" + body + "\n\\end{document}\n"
    )


async def render_pdf(code: str, timeout: int = 150) -> bytes:
    """Compile the (wrapped) snippet; return PDF bytes or raise TikzError."""
    exe = texcompile.tectonic_path()
    if not exe:
        raise TikzError("The LaTeX engine is unavailable on the server right now.")
    doc = wrap(code)

    def _run() -> bytes:
        with tempfile.TemporaryDirectory(prefix="mila_tikz_") as td:
            main = Path(td) / "diagram.tex"
            main.write_text(doc, encoding="utf-8")
            try:
                proc = subprocess.run(
                    [exe, "-X", "compile", "--outdir", td, "diagram.tex"],
                    cwd=td, capture_output=True, text=True, timeout=timeout,
                    env={**os.environ},
                )
            except subprocess.TimeoutExpired:
                raise TikzError(f"TikZ compile timed out after {timeout}s.")
            pdf = Path(td) / "diagram.pdf"
            if proc.returncode != 0 or not pdf.is_file():
                log = (proc.stderr or "") + (proc.stdout or "")
                errs = [l.strip() for l in log.splitlines()
                        if l.strip().lower().startswith(("error", "!"))][:8]
                raise TikzError(
                    "TikZ failed to compile:\n" + ("\n".join(errs) or log[-600:])
                )
            return pdf.read_bytes()

    return await asyncio.to_thread(_run)
