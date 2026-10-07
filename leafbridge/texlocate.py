"""Locate tables and figures in the compiled PDF: which page(s) each lands on.

LaTeX floats have no fixed source->page mapping (the float algorithm decides at
compile time), so we ask LaTeX directly. We compile an *instrumented* copy of the
document that wraps every ``table`` / ``figure`` / ``longtable`` with
``zref-abspage`` start/end labels and records each float's type + number. Reading
the resulting ``.aux`` back gives, per float, the exact **start** and **end**
absolute page numbers:

* a normal single-page float -> start == end (render one page);
* a ``longtable`` that breaks across pages -> start < end (render the range).

This is exact and unambiguous, unlike searching the PDF text for "Table 5" (which
also matches every cross-reference to it).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from dataclasses import dataclass, field

# Prepended immediately before \begin{document}. Only common packages that
# Tectonic fetches on demand. The \providecommand guards let LaTeX re-read the
# .aux on later passes without erroring on our custom \milafloat records.
INSTRUMENT = r"""
%% ---- MiLatexAI float locator (auto-inserted) ----
\usepackage{etoolbox}
\usepackage{zref-user}
\usepackage{zref-abspage}
\providecommand{\milafloat}[4]{}
\newcounter{milainst}
\makeatletter
% Start: remember the float counter, so a float without a caption (which never steps it)
% is written with an empty number instead of borrowing its neighbour's.
\newcommand{\mila@begin}[1]{\stepcounter{milainst}\zlabel{milaS\themilainst}%
  \edef\mila@start{\the\value{#1}}}
% End: the PRINTED number (\thetable, e.g. 2.1 in a thesis), not the raw counter.
\newcommand{\mila@end}[1]{\zlabel{milaE\themilainst}%
  \ifnum\value{#1}=\mila@start\relax
    \protected@write\@auxout{}{\string\milafloat{\themilainst}{#1}{}{}}%
  \else
    \protected@write\@auxout{}{\string\milafloat{\themilainst}{#1}{\csname the#1\endcsname}{}}%
  \fi}
\AtBeginEnvironment{table}{\mila@begin{table}}\AtEndEnvironment{table}{\mila@end{table}}
\AtBeginEnvironment{table*}{\mila@begin{table}}\AtEndEnvironment{table*}{\mila@end{table}}
\AtBeginEnvironment{longtable}{\mila@begin{table}}\AtEndEnvironment{longtable}{\mila@end{table}}
\AtBeginEnvironment{xltabular}{\mila@begin{table}}\AtEndEnvironment{xltabular}{\mila@end{table}}
\AtBeginEnvironment{sidewaystable}{\mila@begin{table}}\AtEndEnvironment{sidewaystable}{\mila@end{table}}
\AtBeginEnvironment{sidewaystable*}{\mila@begin{table}}\AtEndEnvironment{sidewaystable*}{\mila@end{table}}
\AtBeginEnvironment{wraptable}{\mila@begin{table}}\AtEndEnvironment{wraptable}{\mila@end{table}}
\AtBeginEnvironment{SCtable}{\mila@begin{table}}\AtEndEnvironment{SCtable}{\mila@end{table}}
\AtBeginEnvironment{figure}{\mila@begin{figure}}\AtEndEnvironment{figure}{\mila@end{figure}}
\AtBeginEnvironment{figure*}{\mila@begin{figure}}\AtEndEnvironment{figure*}{\mila@end{figure}}
\AtBeginEnvironment{sidewaysfigure}{\mila@begin{figure}}\AtEndEnvironment{sidewaysfigure}{\mila@end{figure}}
\AtBeginEnvironment{sidewaysfigure*}{\mila@begin{figure}}\AtEndEnvironment{sidewaysfigure*}{\mila@end{figure}}
\AtBeginEnvironment{wrapfigure}{\mila@begin{figure}}\AtEndEnvironment{wrapfigure}{\mila@end{figure}}
\AtBeginEnvironment{SCfigure}{\mila@begin{figure}}\AtEndEnvironment{SCfigure}{\mila@end{figure}}
\makeatother
%% ---- end MiLatexAI float locator ----
"""

_DOCSTART = re.compile(r"\\begin\{document\}")
_MILAFLOAT = re.compile(r"\\milafloat\{(\d+)\}\{(\w+)\}\{([^}]*)\}\{([^}]*)\}")
_ZREF = re.compile(r"\\zref@newlabel\{mila([SE])(\d+)\}\{.*?\\abspage\{(\d+)\}")
_NEWLABEL = re.compile(r"\\newlabel\{([^}]+)\}\{\{([^}]*)\}\{([^}]*)\}")
# zref also records the PRINTED page of every float start/end: printed -> absolute page.
_ZREF_PAGE = re.compile(r"\\zref@newlabel\{mila[SE]\d+\}\{.*?\\page\{([^}]*)\}\\abspage\{(\d+)\}")
# hyperref adds the anchor after the caption: {table.caption.9} or {figure.2.1}; its first word is the kind.
_ANCHOR = re.compile(r"\\newlabel\{([^}]+)\}\{\{[^}]*\}\{[^}]*\}\{[^{}]*\}\{(table|figure)\.")
_FLOATNUM = re.compile(r"^[A-Za-z]?\d+(?:\.\d+)*$")


@dataclass
class Float:
    kind: str  # "table" or "figure"
    number: str  # as printed: "4", or "2.1" when numbered within chapters
    start_page: int | None = None
    end_page: int | None = None

    @property
    def pages(self) -> list[int]:
        s = self.start_page if self.start_page is not None else self.end_page
        e = self.end_page if self.end_page is not None else self.start_page
        if s is None:
            return []
        return list(range(min(s, e), max(s, e) + 1))

    @property
    def spans(self) -> bool:
        return len(self.pages) > 1


def instrument(source: str) -> str:
    """Insert the instrumentation just before ``\\begin{document}``."""
    m = _DOCSTART.search(source)
    if not m:
        return source
    return source[: m.start()] + INSTRUMENT + source[m.start() :]


def parse_aux(aux: str) -> tuple[dict[tuple[str, str], Float], dict[str, tuple[str, int | None]]]:
    """Parse an instrumented ``.aux``.

    Returns ``(floats, labels)`` where ``floats`` maps ``(kind, number)`` to a
    :class:`Float`, and ``labels`` maps any user ``\\label`` name to
    ``(number_str, page)`` (handy for locating a float by its label).
    """
    starts: dict[int, int] = {}
    ends: dict[int, int] = {}
    for se, inst, page in _ZREF.findall(aux):
        (starts if se == "S" else ends)[int(inst)] = int(page)
    floats: dict[tuple[str, str], Float] = {}
    for inst, kind, number, _cap in _MILAFLOAT.findall(aux):
        i = int(inst)
        number = number.strip()
        if not number:          # no caption, so no number of its own: not addressable by number
            continue
        floats[(kind, number)] = Float(kind=kind, number=number, start_page=starts.get(i), end_page=ends.get(i))
    labels: dict[str, tuple[str, int | None]] = {}
    printed: dict[str, str] = {}
    for name, num, page in _NEWLABEL.findall(aux):
        if not name.startswith("mila") and "@" not in name:     # skip cleveref's name@cref copies
            labels[name] = (num.strip(), int(page) if page.strip().isdigit() else None)
            printed[name] = page.strip()
    _add_from_labels(aux, floats, labels, printed)
    return floats, labels


def _label_kind(name: str, anchor: str | None) -> str | None:
    if anchor:
        return anchor
    low = name.lower()
    if low.startswith(("tab:", "tab-", "tab_", "table:", "tbl:")):
        return "table"
    if low.startswith(("fig:", "fig-", "fig_", "figure:")):
        return "figure"
    return None


def _add_from_labels(aux: str, floats: dict, labels: dict, printed: dict) -> None:
    """Tables and figures the hooks could not see (some packages, such as xltabular, never run
    them) are still found through their \\label: placed on the label's page. The kind comes
    from hyperref's anchor when there is one, otherwise from a tab:/fig: style label name."""
    to_abs: dict[str, int] = {}
    for page, absolute in _ZREF_PAGE.findall(aux):
        to_abs.setdefault(page.strip(), int(absolute))
    offsets = sorted(a - int(p) for p, a in to_abs.items() if p.isdigit())
    offset = offsets[len(offsets) // 2] if offsets else 0       # front matter shifts arabic pages
    anchors = dict(_ANCHOR.findall(aux))
    for name, (num, _page) in labels.items():
        kind = _label_kind(name, anchors.get(name))
        if kind is None or not _FLOATNUM.match(num) or (kind, num) in floats:
            continue
        page = printed.get(name, "")
        absolute = to_abs.get(page) or (int(page) + offset if page.isdigit() else None)
        if absolute:
            floats[(kind, num)] = Float(kind=kind, number=num, start_page=absolute, end_page=absolute)


def natural(number: str) -> tuple:
    """Sort key for printed numbers: 2.10 after 2.9, A.1 after the numbered chapters."""
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"[.\-]", number))


@dataclass
class LocateResult:
    ok: bool
    floats: dict[tuple[str, str], Float] = field(default_factory=dict)
    labels: dict[str, tuple[str, int | None]] = field(default_factory=dict)
    pdf_path: str | None = None
    message: str = ""
    clean: bool = False         # the compile itself succeeded (exit 0 and a PDF), not just "floats found"
    pages: int | None = None
    note: str = ""              # e.g. "built with a smaller copy of figures/x.png"


def compile_and_locate(
    repo_dir: str, main_rel: str, tectonic: str, cache_dir: str | None = None
) -> LocateResult:
    """Instrument a copy of ``main_rel`` inside ``repo_dir``, compile it with
    Tectonic (keeping the .aux), and return the float->page map + the PDF path.
    Oversized images are compiled from a smaller copy (texmemory.preview_tree); the PDF is
    then placed where it always is, next to the main file in ``repo_dir``."""
    from .texmemory import preview_tree
    with preview_tree(Path(repo_dir)) as (src, note):
        res = _compile_and_locate(str(src), main_rel, tectonic, cache_dir)
        if Path(src) != Path(repo_dir) and res.pdf_path:
            dest = os.path.join(repo_dir, os.path.relpath(res.pdf_path, str(src)))
            shutil.copy2(res.pdf_path, dest)
            res.pdf_path = dest
        res.note = note
        return res


def _compile_and_locate(
    repo_dir: str, main_rel: str, tectonic: str, cache_dir: str | None = None
) -> LocateResult:
    main_path = os.path.join(repo_dir, main_rel)
    if not os.path.isfile(main_path):
        return LocateResult(False, message=f"main file not found: {main_rel}")
    source = open(main_path, encoding="utf-8", errors="replace").read()
    stem = os.path.splitext(os.path.basename(main_rel))[0] + "__mila"
    inst_name = stem + ".tex"
    # Write the instrumented copy alongside the original so \input paths resolve.
    inst_dir = os.path.dirname(main_path) or repo_dir
    with open(os.path.join(inst_dir, inst_name), "w", encoding="utf-8") as fh:
        fh.write(instrument(source))
    env = dict(os.environ)
    if cache_dir:
        env["TECTONIC_CACHE_DIR"] = cache_dir
    try:
        proc = subprocess.run(
            [tectonic, "-X", "compile", "--keep-intermediates", "--outdir", inst_dir, inst_name],
            cwd=inst_dir, env=env, capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return LocateResult(False, message=f"compile failed to run: {exc}")
    if proc.returncode < 0:
        # Killed (usually out of memory): whatever it left behind is incomplete.
        from .texmemory import stopped_message
        return LocateResult(False, message=stopped_message(Path(repo_dir), proc.returncode))
    aux_path = os.path.join(inst_dir, stem + ".aux")
    pdf_path = os.path.join(inst_dir, stem + ".pdf")
    if not os.path.isfile(aux_path):
        tail = (proc.stderr or proc.stdout or "")[-400:]
        return LocateResult(False, message=f"no .aux produced. {tail}")
    floats, labels = parse_aux(open(aux_path, encoding="utf-8", errors="replace").read())
    has_pdf = os.path.isfile(pdf_path)
    pages = None
    if has_pdf:
        try:
            pages = page_count(pdf_path)
        except Exception:  # noqa: BLE001
            pages = None
    return LocateResult(
        ok=bool(floats), floats=floats, labels=labels,
        pdf_path=pdf_path if has_pdf else None,
        message=f"{len(floats)} float(s) located",
        clean=proc.returncode == 0 and has_pdf, pages=pages,
    )


_REFNUM = re.compile(r"(?:table|tab\.?|figure|fig\.?)?\s*#?\s*([A-Za-z]?[\w.\-]*\d[\w.\-]*)\s*$", re.I)


def resolve_number(ref: str, res: "LocateResult", kind: str | None = None) -> str | None:
    """Turn a caller-supplied reference into a float's printed number.

    Accepts a user ``\\label`` (looked up in the parsed .aux), the printed number ("4",
    "2.1"), or a "Table 2.1" / "Fig 3" style string. A bare "1" in a document numbered by
    chapter is used only when exactly one float of ``kind`` ends in ".1". Returns None when
    it can't resolve, or when it is ambiguous; the caller then lists the floats.
    """
    ref = (ref or "").strip()
    if not ref:
        return None
    lab = res.labels.get(ref)
    if lab:
        return lab[0]
    m = _REFNUM.match(ref)
    core = m.group(1) if m else None
    if not core:
        return None
    kinds = [kind] if kind else sorted({k for k, _ in res.floats})
    if any((k, core) in res.floats for k in kinds) or not res.floats:
        return core
    if core.isdigit():
        tails = {n for (k, n) in res.floats if k in kinds and re.split(r"[.\-]", n)[-1] == core}
        if len(tails) == 1:
            return tails.pop()
    return None


def float_listing(res: "LocateResult", kind: str) -> str:
    """A human/LLM-readable list of the floats of ``kind`` with page + label."""
    labels_by_num: dict[str, str] = {}
    prefix = "tab" if kind == "table" else "fig"
    for name, (num, _p) in sorted(res.labels.items()):
        if name.startswith(prefix):
            labels_by_num.setdefault(num, name)
    rows = []
    for (k, n) in sorted(res.floats, key=lambda kn: (kn[0], natural(kn[1]))):
        if k != kind:
            continue
        pg = res.floats[(k, n)].pages
        if not pg:
            continue
        loc = f"p.{pg[0]}" if len(pg) == 1 else f"p.{pg[0]}-{pg[-1]}"
        lab = labels_by_num.get(n)
        rows.append(f"  {kind.title()} {n}: {loc}" + (f"  (\\label {{{lab}}})" if lab else ""))
    if not rows:
        return f"No {kind}s were found in this document."
    return f"{kind.title()}s in this document:\n" + "\n".join(rows)


def render_pages(pdf_path: str, pages: list[int], dpi: int = 150) -> list[bytes]:
    """Render 1-based absolute page numbers of ``pdf_path`` to PNG bytes."""
    import fitz  # PyMuPDF

    out: list[bytes] = []
    doc = fitz.open(pdf_path)
    try:
        for p in pages:
            if 1 <= p <= doc.page_count:
                out.append(doc[p - 1].get_pixmap(dpi=dpi).tobytes("png"))
    finally:
        doc.close()
    return out


def page_count(pdf_path: str) -> int:
    """Number of pages in a PDF (used to validate a requested page number)."""
    import fitz  # PyMuPDF

    doc = fitz.open(pdf_path)
    try:
        return doc.page_count
    finally:
        doc.close()


def _main() -> None:
    import sys
    tex = sys.argv[1]
    tectonic = os.environ.get("TECTONIC_BIN", "tectonic")
    repo = os.path.dirname(os.path.abspath(tex)) or "."
    res = compile_and_locate(repo, os.path.basename(tex), tectonic,
                             cache_dir=os.environ.get("TECTONIC_CACHE_DIR"))
    print("ok:", res.ok, "|", res.message)
    for (kind, num), f in sorted(res.floats.items(), key=lambda kv: (kv[0][0], natural(kv[0][1]))):
        span = f" (spans {f.pages[0]}-{f.pages[-1]})" if f.spans else ""
        print(f"  {kind.title()} {num}: page {f.pages[0] if f.pages else '?'}{span}")
    if len(sys.argv) > 3:
        kind, num = sys.argv[2], sys.argv[3]
        f = res.floats.get((kind, num))
        print(f"\nLOCATE {kind} {num}: pages {f.pages if f else 'not found'}")


if __name__ == "__main__":
    _main()
