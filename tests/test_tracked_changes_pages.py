"""tracked_changes_pdf shows the pages that actually changed, not just the first 8.

latexdiff marks additions in blue and deletions in red; those pages are found in the PDF.
A thesis whose changes are on pages 40 to 42 used to show pages 1 to 8 with nothing on them.
"""

from __future__ import annotations

import asyncio
import subprocess
import warnings

import pytest
from fastmcp import Client

from leafbridge import texdiff
from leafbridge.hosted import create_hosted_server
from leafbridge.service import AccountService
from leafbridge.store import InMemoryStore, TokenCipher, User

warnings.filterwarnings("ignore", category=DeprecationWarning)
fitz = pytest.importorskip("fitz")
BLUE, RED, BLACK = (0, 0, 1), (1, 0, 0), (0, 0, 0)


def _pdf(n: int, marks: dict[int, tuple]) -> bytes:
    doc = fitz.open()
    for i in range(1, n + 1):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page {i} body text", color=BLACK)
        if i in marks:
            page.insert_text((72, 100), "changed words", color=marks[i])
    return doc.tobytes()


def test_marked_pages_are_found():
    pdf = _pdf(12, {9: BLUE, 10: RED, 11: BLUE})
    assert texdiff.changed_pages(pdf) == ([9, 10, 11], 12)
    assert texdiff.changed_pages(_pdf(3, {})) == ([], 3)


def test_page_ranges_read_naturally():
    assert texdiff.page_ranges([3, 4, 5, 9]) == "3-5, 9"
    assert texdiff.page_ranges([7]) == "7" and texdiff.page_ranges([]) == ""


def _world(tmp_path, pdf, monkeypatch):
    remote, seed = tmp_path / "r.git", tmp_path / "seed"
    remote.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", "."], cwd=remote, check=True)
    seed.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=seed, check=True)
    (seed / "main.tex").write_text("\\documentclass{article}\\begin{document}x\\end{document}\n")
    subprocess.run(["git", "add", "-A"], cwd=seed, check=True)
    subprocess.run(["git", "-c", "user.name=S", "-c", "user.email=s@t", "commit", "-qm", "init"], cwd=seed, check=True)
    subprocess.run(["git", "push", "-q", remote.as_uri(), "main"], cwd=seed, check=True)
    store, cipher = InMemoryStore(), TokenCipher(TokenCipher.generate_key())
    asyncio.run(store.upsert_user(User(user_id="u", email="u@x.com", plan="pro")))
    asyncio.run(AccountService(store, cipher).connect_project(
        "u", "https://www.overleaf.com/project/0123456789abcdef01234567", "olp_x", "paper", git_url=remote.as_uri()))

    async def fake_diff(repo, main, old, timeout=240):
        return pdf
    monkeypatch.setattr(texdiff, "diff_pdf", fake_diff)
    return create_hosted_server(store=store, cipher=cipher, auth=False, identity_provider=lambda: ("u", "u@x.com"),
                                base_url="https://milatexai.com", data_dir=tmp_path / "data")


def _call(mcp, args):
    async def go():
        async with Client(mcp) as c:
            r = await c.call_tool("tracked_changes_pdf", args, raise_on_error=False)
            text = " ".join(getattr(b, "text", "") for b in r.content if getattr(b, "type", "") == "text")
            images = sum(1 for b in r.content if getattr(b, "type", "") == "image")
            return r.is_error, text, images
    return asyncio.run(go())


def test_the_changed_pages_are_shown_even_deep_in_a_long_document(tmp_path, monkeypatch):
    mcp = _world(tmp_path, _pdf(60, {40: BLUE, 41: RED, 42: BLUE}), monkeypatch)
    err, text, images = _call(mcp, {"ref": "HEAD"})
    assert not err and images == 3
    assert "Changes are marked on 3 of 60 page(s): 40-42" in text and "Showing page(s) 40-42" in text


def test_more_than_eight_changed_pages_shows_eight_and_says_how_to_see_the_rest(tmp_path, monkeypatch):
    mcp = _world(tmp_path, _pdf(30, {p: BLUE for p in range(5, 20)}), monkeypatch)
    err, text, images = _call(mcp, {"ref": "HEAD"})
    assert not err and images == 8 and "5-19" in text and "pages=[...]" in text
    err, text, images = _call(mcp, {"ref": "HEAD", "pages": [18, 19]})
    assert not err and images == 2 and "Showing page(s) 18-19" in text


def test_without_marked_text_the_first_pages_are_shown_and_it_says_so(tmp_path, monkeypatch):
    mcp = _world(tmp_path, _pdf(12, {}), monkeypatch)
    err, text, images = _call(mcp, {"ref": "HEAD"})
    assert not err and images == 8 and "No marked-up text was found" in text
