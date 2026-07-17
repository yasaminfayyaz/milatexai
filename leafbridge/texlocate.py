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
\newcommand{\mila@begin}{\stepcounter{milainst}\zlabel{milaS\themilainst}}
\newcommand{\mila@end}[1]{\zlabel{milaE\themilainst}%
  \protected@write\@auxout{}{\string\milafloat{\themilainst}{#1}{\the\value{#1}}{}}}
\AtBeginEnvironment{table}{\mila@begin}\AtEndEnvironment{table}{\mila@end{table}}
\AtBeginEnvironment{figure}{\mila@begin}\AtEndEnvironment{figure}{\mila@end{figure}}
\AtBeginEnvironment{longtable}{\mila@begin}\AtEndEnvironment{longtable}{\mila@end{table}}
\AtBeginEnvironment{table*}{\mila@begin}\AtEndEnvironment{table*}{\mila@end{table}}
\AtBeginEnvironment{figure*}{\mila@begin}\AtEndEnvironment{figure*}{\mila@end{figure}}
\makeatother
%% ---- end MiLatexAI float locator ----
"""

_DOCSTART = re.compile(r"\\begin\{document\}")
_MILAFLOAT = re.compile(r"\\milafloat\{(\d+)\}\{(\w+)\}\{(\d+)\}\{([^}]*)\}")
_ZREF = re.compile(r"\\zref@newlabel\{mila([SE])(\d+)\}\{.*?\\abspage\{(\d+)\}")
_NEWLABEL = re.compile(r"\\newlabel\{([^}]+)\}\{\{([^}]*)\}\{(\d+)\}")


@dataclass
class Float:
    kind: str  # "table" or "figure"
    number: int
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


def parse_aux(aux: str) -> tuple[dict[tuple[str, int], Float], dict[str, tuple[str, int]]]:
    """Parse an instrumented ``.aux``.

    Returns ``(floats, labels)`` where ``floats`` maps ``(kind, number)`` to a
    :class:`Float`, and ``labels`` maps any user ``\\label`` name to
    ``(number_str, page)`` (handy for locating a float by its label).
    """
    starts: dict[int, int] = {}
    ends: dict[int, int] = {}
    for se, inst, page in _ZREF.findall(aux):
        (starts if se == "S" else ends)[int(inst)] = int(page)
    floats: dict[tuple[str, int], Float] = {}
    for inst, kind, number, _cap in _MILAFLOAT.findall(aux):
        i = int(inst)
        f = Float(kind=kind, number=int(number), start_page=starts.get(i), end_page=ends.get(i))
        floats[(kind, int(number))] = f
    labels: dict[str, tuple[str, int]] = {}
    for name, num, page in _NEWLABEL.findall(aux):
        if not name.startswith("mila"):
            labels[name] = (num, int(page))
    return floats, labels


@dataclass
class LocateResult:
    ok: bool
    floats: dict[tuple[str, int], Float] = field(default_factory=dict)
    labels: dict[str, tuple[str, int]] = field(default_factory=dict)
    pdf_path: str | None = None
    message: str = ""


def compile_and_locate(
    repo_dir: str, main_rel: str, tectonic: str, cache_dir: str | None = None
) -> LocateResult:
    """Instrument a copy of ``main_rel`` inside ``repo_dir``, compile it with
    Tectonic (keeping the .aux), and return the float->page map + the PDF path."""
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
    aux_path = os.path.join(inst_dir, stem + ".aux")
    pdf_path = os.path.join(inst_dir, stem + ".pdf")
    if not os.path.isfile(aux_path):
        tail = (proc.stderr or proc.stdout or "")[-400:]
        return LocateResult(False, message=f"no .aux produced. {tail}")
    floats, labels = parse_aux(open(aux_path, encoding="utf-8", errors="replace").read())
    return LocateResult(
        ok=bool(floats), floats=floats, labels=labels,
        pdf_path=pdf_path if os.path.isfile(pdf_path) else None,
        message=f"{len(floats)} float(s) located",
    )


_REFNUM = re.compile(r"(?:table|figure|fig\.?|tab\.?)?\s*#?\s*(\d+)\s*$", re.I)


def resolve_number(ref: str, res: "LocateResult") -> int | None:
    """Turn a caller-supplied reference into a float number.

    Accepts a bare number ("4"), a "Table 4" / "Fig 3" style string, or a user
    ``\\label`` (looked up in the parsed .aux). Returns None if it can't resolve,
    in which case the caller should list the available floats.
    """
    ref = (ref or "").strip()
    if not ref:
        return None
    lab = res.labels.get(ref)
    if lab:
        try:
            return int(lab[0])
        except ValueError:
            pass
    if ref.isdigit():
        return int(ref)
    m = _REFNUM.match(ref)
    return int(m.group(1)) if m else None


def float_listing(res: "LocateResult", kind: str) -> str:
    """A human/LLM-readable list of the floats of ``kind`` with page + label."""
    labels_by_num: dict[str, str] = {}
    prefix = "tab" if kind == "table" else "fig"
    for name, (num, _p) in res.labels.items():
        if name.startswith(prefix):
            labels_by_num.setdefault(num, name)
    rows = []
    for (k, n) in sorted(res.floats):
        if k != kind:
            continue
        pg = res.floats[(k, n)].pages
        loc = f"p.{pg[0]}" if len(pg) == 1 else f"p.{pg[0]}-{pg[-1]}"
        lab = labels_by_num.get(str(n))
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


def _main() -> None:
    import sys
    tex = sys.argv[1]
    tectonic = os.environ.get("TECTONIC_BIN", "tectonic")
    repo = os.path.dirname(os.path.abspath(tex)) or "."
    res = compile_and_locate(repo, os.path.basename(tex), tectonic,
                             cache_dir=os.environ.get("TECTONIC_CACHE_DIR"))
    print("ok:", res.ok, "|", res.message)
    for (kind, num), f in sorted(res.floats.items()):
        span = f" (spans {f.pages[0]}-{f.pages[-1]})" if f.spans else ""
        print(f"  {kind.title()} {num}: page {f.pages[0] if f.pages else '?'}{span}")
    if len(sys.argv) > 3:
        kind, num = sys.argv[2], int(sys.argv[3])
        f = res.floats.get((kind, num))
        print(f"\nLOCATE {kind} {num}: pages {f.pages if f else 'not found'}")


if __name__ == "__main__":
    _main()
