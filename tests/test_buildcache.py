"""Compile results reused per exact version (leafbridge/buildcache.py and the four tools).

The promise: never compile the same version twice, and never show a result for a version it
was not built from. Edits made through MiLatexAI AND edits made directly in Overleaf, GitHub
or GitLab (pushed straight to the remote here) must both produce a fresh compile.

A fake LaTeX engine counts compiles and records the text it compiled; the Git remote and the
server are real. The same behaviour with the real engine is covered in tests/e2e/test_compile_cache.py.
"""

from __future__ import annotations

import asyncio
import subprocess
import warnings
from pathlib import Path

import pytest

from leafbridge import buildcache, load, texcompile, texlocate
from leafbridge.hosted import create_hosted_server
from leafbridge.service import AccountService
from leafbridge.store import InMemoryStore, TokenCipher, User

warnings.filterwarnings("ignore", category=DeprecationWarning)
fitz = pytest.importorskip("fitz")
HEX = "0123456789abcdef01234567"
DOC = "\\documentclass{article}\\begin{document}Version one.\\end{document}\n"


def _git(args, cwd):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, p.stderr or p.stdout
    return p.stdout


def _pdf(path: Path, pages: int) -> None:
    doc = fitz.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"page {i + 1}")
    doc.save(str(path))


class FakeEngine:
    """Stands in for Tectonic. `clean` and `plain_ok` decide how compiles turn out."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.located_calls: list[str] = []      # text of main.tex at each page-locating compile
        self.plain_calls = 0
        self.clean = True
        self.plain_ok = True
        self.pages = 3

    def compile_and_locate(self, repo_dir, main, exe, cache_dir=None):
        text = (Path(repo_dir) / main).read_text(encoding="utf-8")
        self.located_calls.append(text)
        pdf = self.tmp / f"out{len(self.located_calls)}.pdf"
        _pdf(pdf, self.pages)
        floats = {("table", 1): texlocate.Float("table", 1, 2, 2), ("figure", 1): texlocate.Float("figure", 1, 3, 3)}
        return texlocate.LocateResult(ok=True, floats=floats, labels={"tab:a": ("1", 2)},
                                      pdf_path=str(pdf), message="2 float(s) located", clean=self.clean, pages=self.pages)

    async def compile_project(self, repo, main, timeout=240):
        self.plain_calls += 1
        if self.plain_ok:
            return texcompile.CompileResult(True, True, main, self.pages, [], 0, f"Compiles cleanly ({self.pages} pages).")
        return texcompile.CompileResult(True, False, main, None, ["! Undefined control sequence.", "l.5 \\foo"], 0,
                                        "Compile FAILED.")


@pytest.fixture
def world(tmp_path, monkeypatch):
    remote, seed = tmp_path / "remote.git", tmp_path / "seed"
    remote.mkdir()
    _git(["init", "--bare", "-b", "main", "."], remote)
    seed.mkdir()
    _git(["init", "-b", "main", "."], seed)
    (seed / "main.tex").write_text(DOC)
    (seed / "other.tex").write_text(DOC.replace("one", "other"))
    _git(["add", "-A"], seed)
    _git(["-c", "user.name=S", "-c", "user.email=s@t", "commit", "-m", "init"], seed)
    _git(["remote", "add", "origin", remote.as_uri()], seed)
    _git(["push", "-u", "origin", "main"], seed)

    eng = FakeEngine(tmp_path)
    monkeypatch.setattr(texcompile, "tectonic_path", lambda: "/fake/tectonic")
    monkeypatch.setattr(texlocate, "compile_and_locate", eng.compile_and_locate)
    monkeypatch.setattr(texcompile, "compile_project", eng.compile_project)

    store = InMemoryStore()
    cipher = TokenCipher(TokenCipher.generate_key())
    asyncio.run(store.upsert_user(User(user_id="u", email="t@x.com", plan="pro")))
    asyncio.run(AccountService(store, cipher).connect_project(
        "u", f"https://www.overleaf.com/project/{HEX}", "olp_x", "paper", git_url=remote.as_uri()))
    asyncio.run(store.upsert_user(User(user_id="f", email="f@x.com", plan="free")))
    asyncio.run(AccountService(store, cipher).connect_project(
        "f", f"https://www.overleaf.com/project/{HEX}", "olp_x", "paper", git_url=remote.as_uri()))

    def server(name="copy1", identity=("u", "t@x.com")):
        return create_hosted_server(store=store, cipher=cipher, auth=False, identity_provider=lambda: identity,
                                    base_url="https://milatexai.com", data_dir=tmp_path / name)

    server.cipher = cipher

    def outside_edit(text: str):
        """An edit made directly in Overleaf / GitHub: a commit pushed straight to the remote."""
        _git(["pull", "-q", "origin", "main"], seed)
        (seed / "main.tex").write_text(text)
        _git(["-c", "user.name=Web", "-c", "user.email=w@t", "commit", "-qam", "edited on the website"], seed)
        _git(["push", "-q", "origin", "main"], seed)

    return eng, server, outside_edit, store


def call(mcp, tool, args=None):
    from fastmcp import Client

    async def go():
        async with Client(mcp) as c:
            r = await c.call_tool(tool, args or {}, raise_on_error=False)
            text = " ".join(getattr(b, "text", "") for b in r.content)
            return r, text, any(getattr(b, "type", "") == "image" for b in r.content)
    return asyncio.run(go())


# --- never compile the same version twice -------------------------------------------------------

def test_a_clean_check_compiles_once_and_says_the_same_as_before(world):
    eng, server, _, _ = world
    mcp = server()
    _, out, _ = call(mcp, "check_compile", {"project": "paper"})
    assert "main.tex: Compiles cleanly (3 pages)." in out
    assert "Table 1: p.2" in out and "Figure 1: p.3" in out and "Labels: tab:a p.2" in out
    assert len(eng.located_calls) == 1 and eng.plain_calls == 0


def test_looking_at_an_unchanged_paper_never_compiles_again(world):
    eng, server, _, _ = world
    mcp = server()
    call(mcp, "check_compile", {"project": "paper"})
    _, out, img = call(mcp, "show_page", {"page": 2, "project": "paper"})
    assert img and "Page 2 of 3." in out
    _, out, img = call(mcp, "show_table", {"table": "1", "project": "paper"})
    assert img and "Table 1: page 2." in out
    _, out, img = call(mcp, "show_figure", {"figure": "1", "project": "paper"})
    assert img and "Figure 1: page 3." in out
    call(mcp, "check_compile", {"project": "paper"})
    assert len(eng.located_calls) == 1 and eng.plain_calls == 0


def test_an_edit_through_milatexai_compiles_the_new_version(world):
    eng, server, _, _ = world
    mcp = server()
    call(mcp, "show_page", {"page": 1, "project": "paper"})
    r, out, _ = call(mcp, "edit_file", {"path": "main.tex", "old_string": "Version one.", "new_string": "Version two.",
                                        "project": "paper"})
    assert not r.is_error, out
    call(mcp, "show_page", {"page": 1, "project": "paper"})
    assert len(eng.located_calls) == 2 and "Version two." in eng.located_calls[-1]


def test_an_edit_made_directly_in_overleaf_is_compiled_immediately(world):
    """The website edit lands well inside the 15 second read window; compiles must not wait it out."""
    eng, server, outside_edit, _ = world
    mcp = server()
    call(mcp, "show_page", {"page": 1, "project": "paper"})
    outside_edit(DOC.replace("Version one.", "Typed on the website."))
    _, out, img = call(mcp, "show_page", {"page": 1, "project": "paper"})
    assert img and len(eng.located_calls) == 2 and "Typed on the website." in eng.located_calls[-1]
    _, out, _ = call(mcp, "check_compile", {"project": "paper"})
    assert "Compiles cleanly" in out and len(eng.located_calls) == 2     # and then it is reused again


def test_each_root_document_is_compiled_on_its_own(world):
    eng, server, _, _ = world
    mcp = server()
    call(mcp, "check_compile", {"project": "paper", "tex": "main.tex"})
    call(mcp, "check_compile", {"project": "paper", "tex": "other.tex"})
    assert len(eng.located_calls) == 2 and "Version other." in eng.located_calls[-1]


# --- errors always come from the real document ---------------------------------------------------

def test_errors_come_from_the_ordinary_compile_of_the_real_document(world):
    eng, server, _, _ = world
    eng.clean, eng.plain_ok = False, False
    mcp = server()
    _, out, _ = call(mcp, "check_compile", {"project": "paper"})
    assert "Compile FAILED." in out and "! Undefined control sequence." in out and "l.5 \\foo" in out
    assert "Where each table" not in out
    assert len(eng.located_calls) == 1 and eng.plain_calls == 1
    call(mcp, "check_compile", {"project": "paper"})                   # both results reused
    assert len(eng.located_calls) == 1 and eng.plain_calls == 1


def test_if_only_the_page_locating_copy_fails_the_paper_still_compiles(world):
    """Our helper packages can clash with a rare preamble. That must never be reported as the user's error."""
    eng, server, _, _ = world
    eng.clean, eng.plain_ok = False, True
    mcp = server()
    _, out, _ = call(mcp, "check_compile", {"project": "paper"})
    assert "Compiles cleanly (3 pages)." in out and "FAILED" not in out


def test_engine_unavailable_is_reported_and_not_remembered(world, monkeypatch):
    eng, server, _, _ = world
    monkeypatch.setattr(texcompile, "tectonic_path", lambda: None)

    async def unavailable(repo, main, timeout=240):
        return texcompile.CompileResult(available=False, ok=False, message="Tectonic is not installed on the server.")
    monkeypatch.setattr(texcompile, "compile_project", unavailable)
    mcp = server()
    _, out, _ = call(mcp, "check_compile", {"project": "paper"})
    assert "Compile check unavailable" in out
    monkeypatch.setattr(texcompile, "tectonic_path", lambda: "/fake/tectonic")
    monkeypatch.setattr(texcompile, "compile_project", eng.compile_project)
    eng.clean = False             # force the ordinary compile, which must not come back "unavailable" from memory
    _, out, _ = call(mcp, "check_compile", {"project": "paper"})
    assert "Compiles cleanly" in out and "unavailable" not in out and eng.plain_calls == 1


# --- copies, limits and housekeeping -------------------------------------------------------------

def test_two_copies_each_compile_once_and_agree(world):
    eng, server, _, _ = world
    a, b = server("copy1"), server("copy2")
    _, out_a, _ = call(a, "check_compile", {"project": "paper"})
    _, out_b, _ = call(b, "check_compile", {"project": "paper"})
    call(a, "show_page", {"page": 1, "project": "paper"})
    call(b, "show_page", {"page": 1, "project": "paper"})
    assert out_a == out_b and len(eng.located_calls) == 2


def test_reused_results_do_not_use_a_free_users_hourly_allowance(world, monkeypatch):
    eng, server, _, store = world
    monkeypatch.setattr(load, "_slots", load.HeavySlots(1, free_per_hour=1))
    mcp = server("copyf", identity=("f", "f@x.com"))
    first, _, _ = call(mcp, "show_page", {"page": 1, "project": "paper"})
    assert not first.is_error
    for _ in range(3):                                                  # allowance is 1, but nothing compiles
        r, out, _ = call(mcp, "show_page", {"page": 1, "project": "paper"})
        assert not r.is_error, out
    assert len(eng.located_calls) == 1


def test_a_refused_compile_is_not_remembered(world, monkeypatch):
    eng, server, _, _ = world
    real = eng.compile_and_locate
    calls = {"n": 0}

    def busy_once(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise load.Busy("MiLatexAI is busy right now. Please try again in a minute.")
        return real(*a, **k)
    monkeypatch.setattr(texlocate, "compile_and_locate", busy_once)
    mcp = server()
    r, out, _ = call(mcp, "show_page", {"page": 1, "project": "paper"})
    assert r.is_error and "busy right now" in out
    r, out, img = call(mcp, "show_page", {"page": 1, "project": "paper"})
    assert not r.is_error and img


def test_the_cache_keeps_only_the_last_few_versions_and_survives_damage(tmp_path):
    cache = buildcache.BuildCache(tmp_path, keep=2)
    built = {"n": 0}

    def maker(i):
        async def build():
            built["n"] += 1
            pdf = tmp_path / f"p{i}.pdf"
            pdf.write_bytes(b"%PDF-1.4 test")
            return buildcache.Located(clean=True, pages=1, pdf_path=str(pdf))
        return build

    for i in range(5):
        asyncio.run(cache.located("proj", f"commit{i}", "main.tex", maker(i)))
    kept = [p for p in (tmp_path / "proj").iterdir() if p.is_dir()]
    assert len(kept) <= 4 and built["n"] == 5
    res, hit = asyncio.run(cache.located("proj", "commit4", "main.tex", maker(9)))
    assert hit and Path(res.pdf_path).read_bytes() == b"%PDF-1.4 test"
    Path(res.pdf_path).unlink()                                         # a damaged entry is rebuilt, not trusted
    res, hit = asyncio.run(cache.located("proj", "commit4", "main.tex", maker(10)))
    assert not hit and Path(res.pdf_path).is_file()


def test_one_compile_at_a_time_per_copy_by_default(monkeypatch):
    import importlib
    monkeypatch.delenv("HEAVY_SLOTS", raising=False)
    monkeypatch.delenv("FREE_MAX_WAIT", raising=False)
    fresh = importlib.reload(load)
    try:
        assert fresh.HEAVY_SLOTS == 1 and fresh.FREE_MAX_WAIT == 60 and fresh.PRO_MAX_WAIT == 120
    finally:
        importlib.reload(load)


# --- each account has its own copy, and nothing is kept for anyone else -------------------------

def _account_dirs(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith("u"))


def test_two_accounts_on_the_same_repository_never_share_a_copy_or_a_result(world, tmp_path):
    eng, server, _, _ = world
    mine, theirs = server("copy1"), server("copy1", identity=("f", "f@x.com"))  # same server, same disk
    _, out_mine, _ = call(mine, "check_compile", {"project": "paper"})
    _, out_theirs, _ = call(theirs, "check_compile", {"project": "paper"})
    assert out_mine == out_theirs
    assert len(eng.located_calls) == 2                       # nothing compiled for one is used for the other
    assert len(_account_dirs(tmp_path / "copy1")) == 2       # one working copy per account


def test_an_account_that_lost_access_cannot_read_through_someone_elses_copy(world, tmp_path):
    import dataclasses
    _, server, _, store = world
    who = {"id": ("u", "t@x.com")}                         # one running server, two people using it
    mcp = create_hosted_server(store=store, cipher=server.cipher, auth=False, identity_provider=lambda: who["id"],
                               base_url="https://milatexai.com", data_dir=tmp_path / "copy1")
    r, out, _ = call(mcp, "read_file", {"path": "main.tex", "project": "paper"})
    assert not r.is_error and "Version one." in out
    # Moments later the other account reads the same repository, but its access no longer
    # works (token revoked, removed from the project...). It must not get the first copy.
    proj = asyncio.run(store.list_projects("f"))[0]
    asyncio.run(store.put_project(dataclasses.replace(proj, git_url=(tmp_path / "gone.git").as_uri())))
    who["id"] = ("f", "f@x.com")
    r, out, _ = call(mcp, "read_file", {"path": "main.tex", "project": "paper"})
    assert r.is_error and "Version one." not in out


def test_disconnecting_deletes_that_accounts_copy_and_compile_results(world, tmp_path):
    _, server, _, _ = world
    mine, theirs = server("copy1"), server("copy1", identity=("f", "f@x.com"))
    call(mine, "check_compile", {"project": "paper"})
    call(theirs, "check_compile", {"project": "paper"})
    builds = tmp_path / "copy1" / "_builds"
    assert len(_account_dirs(tmp_path / "copy1")) == 2 and len(list(builds.iterdir())) == 2
    r, out, _ = call(mine, "disconnect_project", {"project": "paper"})
    assert not r.is_error and "Disconnected" in out
    assert len(_account_dirs(tmp_path / "copy1")) == 1 and len(list(builds.iterdir())) == 1
    r, out, _ = call(theirs, "read_file", {"path": "main.tex", "project": "paper"})
    assert not r.is_error and "Version one." in out           # the other account is untouched


# --- download files are stored encrypted, readable only with the link -------------------------

def test_download_files_are_stored_encrypted_and_open_only_with_the_link(world, monkeypatch):
    import re
    from urllib.parse import urlparse
    from starlette.testclient import TestClient
    from leafbridge import arxivprep
    _, server, _, store = world

    async def no_bbl(repo, main, timeout=240):
        return None
    monkeypatch.setattr(arxivprep, "compile_bbl", no_bbl)
    mcp = server()
    r, out, _ = call(mcp, "arxiv_export", {"project": "paper"})
    assert not r.is_error, out
    link = re.search(r"https://milatexai\.com/dl\?code=\S+", out).group(0)
    (stored,) = store._downloads.values()
    assert not stored.startswith(b"PK") and b"Version one." not in stored      # not a readable zip at rest
    with TestClient(mcp.http_app()) as client:
        got = client.get(urlparse(link).path + "?" + urlparse(link).query)
        assert got.status_code == 200 and got.content.startswith(b"PK")       # the link opens the real zip


def test_a_download_link_from_before_the_change_still_opens(world):
    import json
    from urllib.parse import quote
    from starlette.testclient import TestClient
    _, server, _, store = world
    mcp = server()
    asyncio.run(store.put_download("old.zip", b"PK old bundle"))
    code = server.cipher.encrypt(json.dumps({"k": "dl", "f": "old.zip"}))
    with TestClient(mcp.http_app()) as client:
        got = client.get("/dl?code=" + quote(code, safe=""))
        assert got.status_code == 200 and got.content == b"PK old bundle"


def test_copies_nobody_used_for_a_day_are_deleted_with_their_results(world, tmp_path, monkeypatch):
    import os
    import time as _time
    from leafbridge import hosted
    _, server, _, _ = world
    monkeypatch.setattr(hosted, "IDLE_SWEEP_EVERY", 0.0)
    mine, theirs = server("copy1"), server("copy1", identity=("f", "f@x.com"))
    call(mine, "check_compile", {"project": "paper"})
    call(theirs, "check_compile", {"project": "paper"})
    root, builds = tmp_path / "copy1", tmp_path / "copy1" / "_builds"
    old = _time.time() - 2 * 24 * 3600                       # my copy was last used two days ago
    from leafbridge.git_worker import GitWorker
    proj_dir = root / GitWorker.key_for("u", HEX)
    mine_dir = proj_dir.parent
    os.utime(proj_dir / ".git" / "milatexai-last-use", (old, old))
    call(theirs, "list_files", {"project": "paper"})       # any request runs the check
    assert not mine_dir.exists()                            # deleted, with its compile results
    assert len(_account_dirs(root)) == 1 and len(list(builds.iterdir())) == 1
    r, out, _ = call(mine, "read_file", {"path": "main.tex", "project": "paper"})
    assert not r.is_error and "Version one." in out          # and simply fetched again when needed
