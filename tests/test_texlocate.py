"""Tests for the table/figure page locator (parse the instrumented .aux)."""

from __future__ import annotations

from leafbridge import texlocate


def test_page_count_and_render_pages(tmp_path):
    """page_count reports the right total and render_pages returns clamped PNGs
    (the machinery behind the show_page tool)."""
    import fitz  # PyMuPDF

    doc = fitz.open()
    for i in range(3):
        doc.new_page().insert_text((72, 72), f"Page {i + 1}")
    p = tmp_path / "doc.pdf"
    doc.save(str(p))
    doc.close()

    assert texlocate.page_count(str(p)) == 3
    pngs = texlocate.render_pages(str(p), [2])
    assert len(pngs) == 1 and pngs[0][:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic bytes
    assert texlocate.render_pages(str(p), [99]) == []  # out-of-range page is clamped out


# A representative instrumented .aux (as produced by Tectonic): four floats,
# the last a longtable that spans pages 3-5.
SAMPLE_AUX = r"""
\milafloat{1}{table}{1}{}
\milafloat{2}{figure}{1}{}
\milafloat{3}{table}{2}{}
\milafloat{4}{table}{3}{}
\zref@newlabel{milaS1}{\default{1}\page{1}\abspage{1}}
\zref@newlabel{milaS2}{\default{1}\page{1}\abspage{1}}
\zref@newlabel{milaE1}{\default{1}\page{1}\abspage{1}}
\zref@newlabel{milaE2}{\default{1}\page{1}\abspage{1}}
\zref@newlabel{milaS3}{\default{2}\page{2}\abspage{2}}
\zref@newlabel{milaE3}{\default{2}\page{2}\abspage{2}}
\zref@newlabel{milaS4}{\default{3}\page{3}\abspage{3}}
\zref@newlabel{milaE4}{\default{3}\page{5}\abspage{5}}
\newlabel{tab:one}{{1}{1}}
\newlabel{tab:long}{{3}{3}}
\newlabel{sec:intro}{{1}{1}}
"""


def test_parse_aux_pages_and_spanning():
    floats, labels = texlocate.parse_aux(SAMPLE_AUX)
    assert floats[("table", "1")].pages == [1]
    assert floats[("figure", "1")].pages == [1]
    assert floats[("table", "2")].pages == [2]
    longtab = floats[("table", "3")]
    assert longtab.pages == [3, 4, 5]
    assert longtab.spans is True
    assert floats[("table", "1")].spans is False


def test_parse_aux_labels():
    _floats, labels = texlocate.parse_aux(SAMPLE_AUX)
    assert labels["tab:one"] == ("1", 1)
    assert labels["tab:long"] == ("3", 3)
    # mila* internal labels are excluded
    assert not any(k.startswith("mila") for k in labels)


def test_instrument_inserts_before_document():
    src = r"\documentclass{article}" "\n" r"\begin{document}" "\nhi\n" r"\end{document}"
    out = texlocate.instrument(src)
    assert "milafloat" in out
    assert "zref-abspage" in out
    assert out.index("MiLatexAI float locator") < out.index(r"\begin{document}")


def test_instrument_noop_without_document():
    src = r"\documentclass{article}% no body"
    assert texlocate.instrument(src) == src


def _sample_result():
    floats, labels = texlocate.parse_aux(SAMPLE_AUX)
    return texlocate.LocateResult(ok=True, floats=floats, labels=labels)


def test_resolve_number_by_digit_label_and_phrase():
    res = _sample_result()
    assert texlocate.resolve_number("3", res) == "3"
    assert texlocate.resolve_number("Table 2", res) == "2"
    assert texlocate.resolve_number("Fig 1", res) == "1"
    assert texlocate.resolve_number("tab:long", res) == "3"  # resolved via the \label
    # A purely semantic description cannot be resolved by the server -> None,
    # so the tool falls back to listing the floats for the model to choose.
    assert texlocate.resolve_number("the regulatory comparison table", res) is None
    assert texlocate.resolve_number("", res) is None


def test_float_listing_shows_pages_and_labels():
    out = texlocate.float_listing(_sample_result(), "table")
    assert "Table 1" in out and "Table 3" in out
    assert "p.3-5" in out  # spanning range shown
    assert "tab:long" in out  # label shown


# A thesis: floats numbered within chapters (Table 2.1, 3.1, ...), hyperref-style labels,
# cleveref's extra "@cref" labels, and front matter on roman-numbered pages.
THESIS_AUX = r"""
\milafloat{1}{table}{1.1}{}
\milafloat{2}{table}{2.1}{}
\milafloat{3}{table}{3.1}{}
\milafloat{4}{table}{3.2}{}
\milafloat{5}{figure}{2.1}{}
\milafloat{6}{table}{A.1}{}
\milafloat{7}{table}{2.10}{}
\zref@newlabel{milaS1}{\default{1}\page{5}\abspage{12}}
\zref@newlabel{milaE1}{\default{1}\page{5}\abspage{12}}
\zref@newlabel{milaS2}{\default{1}\page{9}\abspage{16}}
\zref@newlabel{milaE2}{\default{1}\page{9}\abspage{16}}
\zref@newlabel{milaS3}{\default{1}\page{20}\abspage{27}}
\zref@newlabel{milaE3}{\default{1}\page{20}\abspage{27}}
\zref@newlabel{milaS4}{\default{1}\page{22}\abspage{29}}
\zref@newlabel{milaE4}{\default{1}\page{23}\abspage{30}}
\zref@newlabel{milaS5}{\default{1}\page{10}\abspage{17}}
\zref@newlabel{milaE5}{\default{1}\page{10}\abspage{17}}
\zref@newlabel{milaS6}{\default{1}\page{90}\abspage{97}}
\zref@newlabel{milaE6}{\default{1}\page{90}\abspage{97}}
\zref@newlabel{milaS7}{\default{1}\page{14}\abspage{21}}
\zref@newlabel{milaE7}{\default{1}\page{14}\abspage{21}}
\newlabel{tab:baseline_vs_selected}{{3.2}{22}{Baseline}{table.caption.9}{}}
\newlabel{tab:baseline_vs_selected@cref}{{[table][2][3]3.2}{[1][22][]22}}
\newlabel{fig:pareto}{{2.1}{10}{Pareto}{figure.caption.4}{}}
\newlabel{tab:abbrev}{{1.1}{v}{Abbreviations}{table.caption.1}{}}
"""


def _thesis():
    floats, labels = texlocate.parse_aux(THESIS_AUX)
    return texlocate.LocateResult(ok=True, floats=floats, labels=labels)


def test_a_thesis_keeps_every_chapter_table_apart():
    res = _thesis()
    assert sorted(n for k, n in res.floats if k == "table") == ["1.1", "2.1", "2.10", "3.1", "3.2", "A.1"]
    assert res.floats[("table", "2.1")].pages == [16] and res.floats[("table", "3.1")].pages == [27]
    assert res.labels["tab:abbrev"] == ("1.1", None)                 # roman page "v": page unknown
    assert "tab:baseline_vs_selected@cref" not in res.labels


def test_a_thesis_resolves_labels_printed_numbers_and_unique_bare_numbers():
    res = _thesis()
    assert texlocate.resolve_number("tab:baseline_vs_selected", res, "table") == "3.2"
    assert texlocate.resolve_number("fig:pareto", res, "figure") == "2.1"
    assert texlocate.resolve_number("2.1", res, "table") == "2.1"
    assert texlocate.resolve_number("Table 3.1", res, "table") == "3.1"
    assert texlocate.resolve_number("A.1", res, "table") == "A.1"
    assert texlocate.resolve_number("10", res, "table") == "2.10"      # only one table ends in .10
    assert texlocate.resolve_number("1", res, "table") is None         # 1.1, 2.1, 3.1, A.1: ambiguous
    assert texlocate.resolve_number("1", res, "figure") == "2.1"        # the only figure


def test_a_thesis_listing_is_in_reading_order_with_labels():
    out = texlocate.float_listing(_thesis(), "table")
    order = [line.split(":")[0].strip() for line in out.splitlines()[1:]]
    assert order == ["Table 1.1", "Table 2.1", "Table 2.10", "Table 3.1", "Table 3.2", "Table A.1"]
    assert r"Table 3.2: p.29-30  (\label {tab:baseline_vs_selected})" in out


def test_the_instrument_records_the_printed_number():
    out = texlocate.instrument("\\documentclass{report}\n\\begin{document}\nx\n\\end{document}")
    assert r"\csname the#1\endcsname" in out and r"\the\value{#1}" not in out
