"""Optional LaTeX compile-check via the Tectonic engine.

If a ``tectonic`` binary is available, LeafBridge can build a project locally and
report whether it compiles plus any hard errors — so an edit can be verified
before (or after) it reaches Overleaf. Entirely optional: with no engine
installed the check degrades to a clear message rather than failing.

Tectonic is self-contained (one binary, fetches TeX packages on demand), which
makes it a good fit for a server that must not carry a full TeX Live install.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

_TEX_ERROR = re.compile(r"^(error:|!)", re.IGNORECASE)
_PAGES = re.compile(r"Output written on \S+ \((\d+) pages?")


def tectonic_path() -> str | None:
    """Locate a tectonic binary: env override, LeafBridge's tools dir, then PATH."""
    override = os.environ.get("LEAFBRIDGE_TECTONIC")
    if override and Path(override).exists():
        return override
    local = os.environ.get("LOCALAPPDATA")
    if local:
        exe = Path(local) / "LeafBridge" / "tools" / "tectonic" / "tectonic.exe"
        if exe.exists():
            return str(exe)
    return shutil.which("tectonic")


def find_main_tex(repo: Path) -> str | None:
    """Find the root .tex file (one containing \\documentclass and
    \\begin{document}), preferring conventional root names."""
    root_names = {"main.tex", "root.tex", "thesis.tex", "paper.tex", "manuscript.tex"}
    best: tuple[int, int, str] | None = None
    for p in repo.rglob("*.tex"):
        if ".git" in p.parts:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "\\documentclass" in text and "\\begin{document}" in text:
            rel = p.relative_to(repo).as_posix()
            score = (10 if p.name.lower() in root_names else 0, -len(rel), rel)
            if best is None or score > best:
                best = score
    return best[2] if best else None


@dataclass
class CompileResult:
    available: bool
    ok: bool
    main_tex: str | None = None
    pages: int | None = None
    errors: list[str] = field(default_factory=list)
    warning_count: int = 0
    message: str = ""


async def compile_project(repo: Path, main_tex: str, timeout: int = 240) -> CompileResult:
    exe = tectonic_path()
    if not exe:
        return CompileResult(
            available=False,
            ok=False,
            message="Tectonic (a local LaTeX engine) is not installed on the server.",
        )
    return await asyncio.to_thread(_compile_sync, exe, repo, main_tex, timeout)


def _compile_sync(exe: str, repo: Path, main_tex: str, timeout: int) -> CompileResult:
    with tempfile.TemporaryDirectory(prefix="lb_compile_") as outdir:
        try:
            proc = subprocess.run(
                [exe, "-X", "compile", "--outdir", outdir, "--keep-logs", "--", main_tex],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return CompileResult(True, False, main_tex, message=f"Compile timed out after {timeout}s.")
        except OSError as exc:
            return CompileResult(True, False, main_tex, message=f"Could not run tectonic: {exc}")

        log = (proc.stderr or "") + "\n" + (proc.stdout or "")
        stem = Path(main_tex).stem
        logfile = Path(outdir) / f"{stem}.log"
        full = log + ("\n" + logfile.read_text(encoding="utf-8", errors="replace") if logfile.exists() else "")
        pdf = Path(outdir) / f"{stem}.pdf"

        errors, seen = [], set()
        for line in full.splitlines():
            s = line.strip()
            if _TEX_ERROR.match(s) and s not in seen:
                seen.add(s)
                errors.append(s)
        warnings = sum(1 for line in log.splitlines() if line.strip().lower().startswith("warning:"))
        pm = _PAGES.search(full)
        pages = int(pm.group(1)) if pm else None

        ok = proc.returncode == 0 and pdf.exists()
        if ok:
            message = f"Compiles cleanly ({pages} pages)." if pages else "Compiles cleanly."
        else:
            message = "Compile FAILED."
        return CompileResult(True, ok, main_tex, pages, errors[:20], warnings, message)
