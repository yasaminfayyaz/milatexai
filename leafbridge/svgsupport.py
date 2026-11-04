"""SVG figures in compile checks.

The LaTeX engine here (Tectonic) reads PDF, PNG and JPEG figures but not SVG, and it cannot run
Inkscape the way Overleaf does for the ``svg`` package. So a project that uses SVG would fail to
compile on our server even though it compiles in Overleaf.

``prepare`` fixes that inside the temporary copy that ``texmemory.preview_tree`` already makes for
compiles: each SVG is converted to a PDF next to it (vector, so it stays sharp), and the copy's
``.tex`` files are adjusted so ``\\includesvg`` and ``\\includegraphics{x.svg}`` find those PDFs.
The project itself is never changed. Conversion runs in a separate process (``rsvg-convert``) with a
time limit; it does not fetch anything from the network or read files outside the SVG's own folder.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

MAX_SVG_BYTES = 8 * 1024 * 1024          # a bigger SVG is not a figure; it would only slow the compile
MAX_SVGS = 60
CONVERT_TIMEOUT = 30                     # seconds per file
_DPI = "96"                              # 1 px = 0.75 pt, the same as Inkscape, so figures print at the size the author sees

# The svg package, replaced by a stand-in that includes the converted PDF.
_SHIM = (r"\usepackage{graphicx}"
         r"\providecommand{\includesvg}[2][]{\includegraphics[#1]{#2}}"
         r"\providecommand{\svgpath}[1]{\graphicspath{#1}}"
         r"\providecommand{\svgsetup}[1]{}")
_USE_PKG = re.compile(r"\\usepackage\s*(\[[^\]]*\])?\s*\{([^}]*)\}")
_EXPLICIT = re.compile(r"(\\(?:includegraphics|includesvg)\*?\s*(?:\[[^\]]*\])?\s*\{[^}]*?)\.svg(\s*\})", re.IGNORECASE)
_SVG_OPTS = re.compile(r"(\\includesvg\*?\s*)\[([^\]]*)\]")
_INKSCAPE_KEY = re.compile(r"\s*inkscape\w*\s*=\s*(?:\{[^}]*\}|[^,\]]*)\s*(?:,|$)")


def converter_path() -> str | None:
    override = os.environ.get("LEAFBRIDGE_RSVG")
    if override and Path(override).exists():
        return override
    return shutil.which("rsvg-convert")


def _svg_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".svg" and ".git" not in p.parts)


def _tex_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.tex") if ".git" not in p.parts]


def needed(repo: Path) -> bool:
    """True if the project has an SVG file and some .tex file mentions svg at all."""
    repo = Path(repo)
    if not _svg_files(repo):
        return False
    for p in _tex_files(repo):
        try:
            if "svg" in p.read_text(encoding="utf-8", errors="replace").lower():
                return True
        except OSError:
            continue
    return False


def rewrite_tex(text: str) -> tuple[str, int]:
    """Point a document at converted SVGs. Returns (new text, how many \\includegraphics calls named
    an .svg file directly). Only the compile copy is ever rewritten."""
    def package(m: re.Match) -> str:
        opts, names = m.group(1) or "", [n.strip() for n in m.group(2).split(",")]
        if "svg" not in names:
            return m.group(0)
        rest = [n for n in names if n and n != "svg"]
        return (f"\\usepackage{opts}{{{','.join(rest)}}}" if rest else "") + _SHIM

    def options(m: re.Match) -> str:
        kept = _INKSCAPE_KEY.sub("", m.group(2)).strip().strip(",")
        return f"{m.group(1)}[{kept}]" if kept else m.group(1)

    text = _USE_PKG.sub(package, text)
    text = _SVG_OPTS.sub(options, text)
    direct = 0

    def explicit(m: re.Match) -> str:
        nonlocal direct
        direct += "includegraphics" in m.group(1)
        return f"{m.group(1)}.pdf{m.group(2)}"

    text = _EXPLICIT.sub(explicit, text)
    return text, direct


def _convert(exe: str, svg: Path, pdf: Path) -> str | None:
    """Convert one file. None on success, otherwise a short reason."""
    try:
        if svg.stat().st_size > MAX_SVG_BYTES:
            return "the file is larger than 8 MB"
        done = subprocess.run([exe, "-f", "pdf", "-d", _DPI, "-p", _DPI, "-o", str(pdf), str(svg)],
                              capture_output=True, text=True, timeout=CONVERT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"it took longer than {CONVERT_TIMEOUT} seconds"
    except OSError:
        return "the converter could not be started"
    if done.returncode != 0 or not pdf.exists() or pdf.stat().st_size == 0:
        pdf.unlink(missing_ok=True)
        return "it is not a valid SVG file"
    return None


def prepare(src: Path) -> str:
    """Convert every SVG under ``src`` (a temporary copy) and adjust its .tex files. Returns the note
    for the user, or "" if there was nothing to say."""
    src = Path(src)
    svgs = _svg_files(src)
    if not svgs:
        return ""
    exe = converter_path()
    if not exe:
        return ("Note: this project has SVG figures, but the compile server has no SVG converter right now, "
                "so they could not be included in this check. This is not an error in your paper.")
    done, failed = [], []
    for svg in svgs[:MAX_SVGS]:
        pdf = svg.with_suffix(".pdf")
        rel = svg.relative_to(src).as_posix()
        if pdf.exists():
            continue                                  # a PDF of the same name is already there: use that one
        reason = _convert(exe, svg, pdf)
        (failed.append((rel, reason)) if reason else done.append(rel))
    direct = 0
    for tex in _tex_files(src):
        try:
            text = tex.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        new, n = rewrite_tex(text)
        direct += n
        if new != text:
            tex.write_text(new, encoding="utf-8")
    parts = []
    if done:
        names = ", ".join(done[:4]) + (f" and {len(done) - 4} more" if len(done) > 4 else "")
        parts.append(f"Note: {names} {'was' if len(done) == 1 else 'were'} converted to PDF for this check, "
                     "because the compile server cannot read SVG directly. Your files are unchanged.")
    if failed:
        parts.append("Could not convert " + "; ".join(f"{rel} ({why})" for rel, why in failed[:4]) + ".")
    if len(svgs) > MAX_SVGS:
        parts.append(f"Only the first {MAX_SVGS} SVG files were converted.")
    if direct:
        parts.append("Overleaf itself does not accept \\includegraphics on an .svg file: in Overleaf use "
                     "\\usepackage{svg} with \\includesvg{figures/name} (no extension), or include a PDF or PNG.")
    return " ".join(parts)
