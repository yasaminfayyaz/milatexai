"""SVG figures: converted to PDF inside the compile copy only, with the .tex files adjusted to match."""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
from pathlib import Path

import pytest

from leafbridge import svgsupport, texcompile, texmemory

SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="30"><rect width="80" height="30" fill="#ccc"/></svg>'


def tiny_pdf(width: int = 80, height: int = 30) -> bytes:
    """A valid one-page PDF with a grey box, built by hand so the tests need no PDF library."""
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] /Contents 4 0 R /Resources << >> >>".encode()]
    stream = f"0.8 g 0 0 {width} {height} re f".encode()
    objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


@pytest.fixture
def fake_converter(monkeypatch):
    """Stands in for rsvg-convert: writes a small PDF unless the SVG is marked broken."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        svg, pdf = Path(cmd[-1]), Path(cmd[cmd.index("-o") + 1])
        if b"BROKEN" in svg.read_bytes():
            return subprocess.CompletedProcess(cmd, 1, "", "parse error")
        pdf.write_bytes(tiny_pdf())
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(svgsupport, "converter_path", lambda: "/usr/bin/rsvg-convert")
    monkeypatch.setattr(svgsupport.subprocess, "run", fake_run)
    return calls


def project(tmp_path, tex, files=None):
    repo = tmp_path / "repo"
    (repo / "figures").mkdir(parents=True)
    (repo / "main.tex").write_text(tex, encoding="utf-8")
    for name, data in (files or {"figures/a.svg": SVG}).items():
        (repo / name).write_text(data, encoding="utf-8") if isinstance(data, str) else (repo / name).write_bytes(data)
    return repo


def tree_digest(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}


# -- rewriting the document -------------------------------------------------

def test_the_svg_package_is_replaced_by_a_stand_in_that_includes_the_pdf():
    new, direct = svgsupport.rewrite_tex("\\usepackage{svg}\n\\includesvg[width=\\linewidth]{figures/a}\n")
    assert "{svg}" not in new and "\\providecommand{\\includesvg}[2][]{\\includegraphics[#1]{#2}}" in new
    assert "\\includesvg[width=\\linewidth]{figures/a}" in new and direct == 0


def test_other_packages_in_the_same_line_are_kept_with_their_options():
    new, _ = svgsupport.rewrite_tex("\\usepackage[final]{graphicx,svg,xcolor}")
    assert new.startswith("\\usepackage[final]{graphicx,xcolor}\\usepackage{graphicx}")


def test_svg_package_options_are_dropped_and_inkscape_keys_removed_from_includesvg():
    new, _ = svgsupport.rewrite_tex("\\usepackage[inkscapelatex=false]{svg}\n\\includesvg[inkscapelatex=false,width=5cm]{x}\n"
                                    "\\includesvg[inkscapelatex=false]{y}")
    assert "inkscape" not in new.replace("Inkscape", "")
    assert "\\includesvg[width=5cm]{x}" in new and "\\includesvg{y}" in new


def test_a_direct_svg_reference_points_at_the_converted_pdf_and_is_counted():
    new, direct = svgsupport.rewrite_tex("\\includegraphics[width=3cm]{figures/a.svg}\n\\includegraphics{b.SVG}\n")
    assert new == "\\includegraphics[width=3cm]{figures/a.pdf}\n\\includegraphics{b.pdf}\n" and direct == 2


def test_text_without_svg_is_left_alone():
    text = "\\usepackage{graphicx}\n\\includegraphics{figures/a.png}\nSee svg-like words in prose.\n"
    assert svgsupport.rewrite_tex(text) == (text, 0)


# -- deciding and converting ------------------------------------------------

def test_only_projects_that_use_svg_need_the_copy(tmp_path):
    assert not svgsupport.needed(project(tmp_path / "a", "\\documentclass{article}", {"figures/a.png": b"x"}))
    assert not svgsupport.needed(project(tmp_path / "b", "\\documentclass{article}\\begin{document}x\\end{document}"))
    assert svgsupport.needed(project(tmp_path / "c", "\\usepackage{svg}"))


def test_converts_each_svg_to_a_pdf_beside_it_and_adjusts_the_tex(tmp_path, fake_converter):
    src = project(tmp_path, "\\usepackage{svg}\n\\includesvg{figures/a}\n\\includegraphics{figures/a.svg}\n")
    note = svgsupport.prepare(src)
    assert (src / "figures" / "a.pdf").read_bytes().startswith(b"%PDF")
    assert (src / "figures" / "a.svg").exists()
    text = (src / "main.tex").read_text()
    assert "{figures/a.pdf}" in text and "figures/a.svg" not in text
    assert "figures/a.svg was converted to PDF for this check" in note and "Your files are unchanged" in note
    assert "does not accept \\includegraphics on an .svg" in note               # the Overleaf warning
    assert fake_converter[0][1:5] == ["-f", "pdf", "-d", "96"]                  # 96 dpi: the size the author sees


def test_no_overleaf_warning_when_only_includesvg_is_used(tmp_path, fake_converter):
    src = project(tmp_path, "\\usepackage{svg}\n\\includesvg{figures/a}\n")
    assert "Overleaf" not in svgsupport.prepare(src)


def test_a_broken_svg_is_reported_and_does_not_stop_the_others(tmp_path, fake_converter):
    src = project(tmp_path, "\\includesvg{figures/a}", {"figures/a.svg": SVG, "figures/bad.svg": "BROKEN"})
    note = svgsupport.prepare(src)
    assert (src / "figures" / "a.pdf").exists() and not (src / "figures" / "bad.pdf").exists()
    assert "figures/bad.svg (it is not a valid SVG file)" in note and "figures/a.svg was converted" in note


def test_a_pdf_that_is_already_there_is_never_overwritten(tmp_path, fake_converter):
    src = project(tmp_path, "\\includesvg{figures/a}", {"figures/a.svg": SVG, "figures/a.pdf": b"%PDF-mine"})
    svgsupport.prepare(src)
    assert (src / "figures" / "a.pdf").read_bytes() == b"%PDF-mine" and fake_converter == []


def test_a_huge_svg_is_skipped_not_converted(tmp_path, fake_converter, monkeypatch):
    monkeypatch.setattr(svgsupport, "MAX_SVG_BYTES", 10)
    note = svgsupport.prepare(project(tmp_path, "\\includesvg{figures/a}"))
    assert "larger than 8 MB" in note and fake_converter == []


def test_a_slow_converter_is_stopped(tmp_path, monkeypatch):
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 30)
    monkeypatch.setattr(svgsupport, "converter_path", lambda: "/x")
    monkeypatch.setattr(svgsupport.subprocess, "run", slow)
    assert "longer than 30 seconds" in svgsupport.prepare(project(tmp_path, "\\includesvg{figures/a}"))


def test_without_a_converter_the_note_says_so_plainly(tmp_path, monkeypatch):
    monkeypatch.setattr(svgsupport, "converter_path", lambda: None)
    note = svgsupport.prepare(project(tmp_path, "\\includesvg{figures/a}"))
    assert "no SVG converter" in note and "not an error in your paper" in note


# -- inside the compile copy -------------------------------------------------

def test_the_project_itself_is_never_touched(tmp_path, fake_converter):
    repo = project(tmp_path, "\\usepackage{svg}\n\\includesvg{figures/a}\n")
    before = tree_digest(repo)
    with texmemory.preview_tree(repo) as (src, note):
        assert src != repo and (src / "figures" / "a.pdf").exists() and "converted to PDF" in note
        assert not (repo / "figures" / "a.pdf").exists() and "{svg}" in (repo / "main.tex").read_text()
        copy_root = src.parent
    assert not copy_root.exists() and tree_digest(repo) == before


def test_without_svg_the_project_itself_is_used(tmp_path):
    repo = project(tmp_path, "\\includegraphics{figures/a.png}", {"figures/a.png": b"x"})
    with texmemory.preview_tree(repo) as (src, note):
        assert src == repo and note == ""


def test_the_compile_runs_in_the_copy_and_reports_the_note(tmp_path, fake_converter, monkeypatch):
    repo = project(tmp_path, "\\documentclass{article}\\usepackage{svg}\\begin{document}\\includesvg{figures/a}\\end{document}")
    seen = {}

    def fake_compile(exe, src, main_tex, timeout):
        seen["tex"] = (Path(src) / main_tex).read_text()
        seen["pdf"] = (Path(src) / "figures" / "a.pdf").exists()
        return texcompile.CompileResult(True, True, main_tex)

    monkeypatch.setattr(texcompile, "_compile_in", fake_compile)
    res = texcompile._compile_sync("tectonic", repo, "main.tex", 60)
    assert seen["pdf"] and "{svg}" not in seen["tex"] and "converted to PDF" in res.note


# -- the real tools, when they are installed ----------------------------------

@pytest.mark.skipif(not svgsupport.converter_path(), reason="rsvg-convert is not installed here")
def test_a_real_svg_becomes_a_real_pdf(tmp_path):
    src = project(tmp_path, "\\includesvg{figures/a}")
    note = svgsupport.prepare(src)
    assert (src / "figures" / "a.pdf").read_bytes().startswith(b"%PDF") and "converted" in note


@pytest.mark.skipif(not texcompile.tectonic_path(), reason="tectonic is not installed here")
def test_tectonic_really_compiles_the_rewritten_document(tmp_path, fake_converter):
    """The TeX side for real: \\usepackage{svg}, \\includesvg and a direct \\includegraphics on an .svg
    all build once their SVGs are PDFs."""
    repo = project(tmp_path, "\\documentclass{article}\n\\usepackage[inkscapelatex=false]{svg}\n\\begin{document}\n"
                             "\\includesvg[width=3cm]{figures/a}\n\\includegraphics[width=3cm]{figures/a.svg}\n"
                             "\\end{document}\n")
    res = asyncio.run(texcompile.compile_project(repo, "main.tex"))
    assert res.ok, res.errors
    assert "converted to PDF" in res.note
