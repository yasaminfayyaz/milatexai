"""Tests for the table/figure page locator (parse the instrumented .aux)."""

from __future__ import annotations

from leafbridge import texlocate

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
    assert floats[("table", 1)].pages == [1]
    assert floats[("figure", 1)].pages == [1]
    assert floats[("table", 2)].pages == [2]
    longtab = floats[("table", 3)]
    assert longtab.pages == [3, 4, 5]
    assert longtab.spans is True
    assert floats[("table", 1)].spans is False


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
