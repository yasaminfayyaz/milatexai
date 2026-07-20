"""Citation toolkit: verified BibTeX fetching and textual citation checks.

``fetch_bibtex`` resolves a DOI (via doi.org content negotiation) or an arXiv id
(via arxiv.org's bibtex endpoint) to REAL BibTeX, so the .bib entry is verified
against the registry of record rather than trusted from an LLM's memory, which
is the anti-hallucination guarantee.
"""

from __future__ import annotations

import re

_DOI = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/\S+)$", re.I)
_ARXIV = re.compile(
    r"^(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv:)?(\d{4}\.\d{4,5}(?:v\d+)?|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})$",
    re.I,
)
_BIB_KEY = re.compile(r"@\s*(\w+)\s*[{(]\s*([^,\s]+)\s*,", re.M)
_CITE = re.compile(
    r"\\(?:cite|citep|citet|citeauthor|citeyear|autocite|parencite|textcite|footcite|Cite)"
    r"\*?(?:\[[^\]]*\]){0,2}\{([^}]+)\}"
)


class CitationError(Exception):
    pass


def classify(ref: str) -> tuple[str, str]:
    """('doi'|'arxiv', normalized id) or raise CitationError."""
    ref = (ref or "").strip()
    m = _DOI.match(ref)
    if m:
        return "doi", m.group(1)
    m = _ARXIV.match(ref)
    if m:
        return "arxiv", m.group(1)
    raise CitationError(
        "Give a DOI (10.xxxx/...) or an arXiv id (2301.01234). For a paper you "
        "only know by title, find its DOI first."
    )


async def fetch_bibtex(ref: str) -> str:
    """Fetch verified BibTeX for a DOI or arXiv id. Raises CitationError."""
    import aiohttp

    kind, ident = classify(ref)
    if kind == "doi":
        url, headers = f"https://doi.org/{ident}", {"Accept": "application/x-bibtex"}
    else:
        url, headers = f"https://arxiv.org/bibtex/{ident}", {}
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url, headers=headers, allow_redirects=True) as resp:
                text = await resp.text()
                status = resp.status
    except aiohttp.ClientError as exc:
        raise CitationError(f"Could not reach the citation registry: {exc}") from exc
    if status != 200 or "@" not in text:
        raise CitationError(
            f"No BibTeX found for {ident!r} (HTTP {status}). Double-check the id."
        )
    return text.strip()


def bib_keys(bib_text: str) -> set[str]:
    """Entry keys in a .bib file (excluding @string/@comment/@preamble)."""
    skip = {"string", "comment", "preamble"}
    return {k for t, k in _BIB_KEY.findall(bib_text) if t.lower() not in skip}


def entry_key(bibtex_entry: str) -> str | None:
    m = _BIB_KEY.search(bibtex_entry)
    return m.group(2) if m else None


def cite_keys(tex_text: str) -> set[str]:
    """Every key referenced by a \\cite-family command."""
    keys: set[str] = set()
    for group in _CITE.findall(tex_text):
        keys.update(k.strip() for k in group.split(",") if k.strip())
    return keys
