"""Running several copies of the server at once (scale out).

Each copy has its own memory and its own disk. Only shared storage (the store) is
common to all of them. These tests build two real copies of the hosted server on
two separate cache directories, sharing one store and one Git remote, and check
the three things that broke when a request landed on a different copy:

  1. a download link made by one copy must open on another,
  2. after one copy pushes an edit, a read on another copy must see it at once,
  3. commit counting must not lose counts (covered against real Azure storage in
     it_azure_store.py; here the in-memory version keeps the same contract).
"""

from __future__ import annotations

import asyncio
import io
import subprocess
import warnings
import zipfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from leafbridge import arxivprep, git_worker
from leafbridge.config import ProjectConfig
from leafbridge.git_worker import GitWorker
from leafbridge.hosted import create_hosted_server
from leafbridge.service import AccountService
from leafbridge.store import InMemoryStore, TokenCipher, User

warnings.filterwarnings("ignore", category=DeprecationWarning)
HEX = "0123456789abcdef01234567"
OVERLEAF_URL = f"https://www.overleaf.com/project/{HEX}"


def _git(args, cwd):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, p.stderr or p.stdout
    return p.stdout


def _remote(tmp_path: Path, text: str = "Words.") -> Path:
    remote, seed = tmp_path / "remote.git", tmp_path / "seed"
    remote.mkdir(parents=True)
    _git(["init", "--bare", "-b", "main", "."], remote)
    seed.mkdir(parents=True)
    _git(["init", "-b", "main", "."], seed)
    (seed / "main.tex").write_text("\\documentclass{article}\\begin{document}" + text + "\\end{document}\n")
    _git(["add", "-A"], seed)
    _git(["-c", "user.name=S", "-c", "user.email=s@t", "commit", "-m", "init"], seed)
    _git(["remote", "add", "origin", remote.as_uri()], seed)
    _git(["push", "-u", "origin", "main"], seed)
    return remote


def _two_copies(tmp_path: Path):
    remote = _remote(tmp_path)
    store = InMemoryStore()
    cipher = TokenCipher(TokenCipher.generate_key())
    asyncio.run(store.upsert_user(User(user_id="u", email="t@x.com", plan="pro")))
    asyncio.run(AccountService(store, cipher).connect_project("u", OVERLEAF_URL, "olp_x", "paper", git_url=remote.as_uri()))
    copies = [create_hosted_server(store=store, cipher=cipher, auth=False, identity_provider=lambda: ("u", "t@x.com"),
                                   base_url="https://milatexai.com", data_dir=tmp_path / f"copy{i}")
              for i in (1, 2)]
    for c in copies:
        c._milatexai_cipher = cipher
    return copies, store, remote


def _call(mcp, tool, args):
    from fastmcp import Client

    async def go():
        async with Client(mcp) as c:
            r = await c.call_tool(tool, args, raise_on_error=False)
            return r, " ".join(getattr(x, "text", "") for x in r.content)
    return asyncio.run(go())


# --- 1. download links ------------------------------------------------------------------------------

def test_a_download_link_made_by_one_copy_opens_on_the_other(tmp_path, monkeypatch):
    (a, b), _store, _remote_ = _two_copies(tmp_path)

    async def fake_bbl(repo, main, timeout=240):
        return "THE BBL"
    monkeypatch.setattr(arxivprep, "compile_bbl", fake_bbl)
    _, out = _call(a, "arxiv_export", {})
    url = next(w for w in out.split() if "/dl?code=" in w)
    path_q = url.split("milatexai.com", 1)[1]
    with TestClient(b.http_app(), base_url="https://testserver") as client:
        resp = client.get(path_q)
    assert resp.status_code == 200, resp.text[:200]
    z = zipfile.ZipFile(io.BytesIO(resp.content))
    assert z.read("main.bbl") == b"THE BBL" and "main.tex" in z.namelist()
    assert resp.headers["content-disposition"].startswith("attachment")
    assert not (tmp_path / "copy1" / "mila_dl").exists()     # nothing parked on a copy's own disk


def test_a_missing_bundle_gives_a_clear_page(tmp_path):
    (a, _b), _store, _r = _two_copies(tmp_path)
    import json
    from urllib.parse import quote
    code = a._milatexai_cipher.encrypt(json.dumps({"k": "dl", "f": "not-there.zip"}))
    with TestClient(a.http_app(), base_url="https://testserver") as client:
        assert client.get("/dl?code=garbage").status_code == 400
        gone = client.get("/dl?code=" + quote(code, safe=""))
    assert gone.status_code == 410 and "no longer available" in gone.text


# --- 2. reads right after another copy's write ------------------------------------------------------

def test_a_read_on_another_copy_sees_an_edit_immediately(tmp_path):
    (a, b), _store, _r = _two_copies(tmp_path)
    _, first = _call(b, "read_file", {"path": "main.tex"})          # copy B now has a fresh working copy
    assert "Words." in first
    r, out = _call(a, "edit_file", {"path": "main.tex", "old_string": "Words.", "new_string": "Changed by copy A."})
    assert not r.is_error, out
    _, seen = _call(b, "read_file", {"path": "main.tex"})           # well inside the 15 second refresh window
    assert "Changed by copy A." in seen and "Words." not in seen


def test_without_the_shared_record_the_other_copy_would_be_stale(tmp_path, monkeypatch):
    """Proves the test above tests something: drop the shared record and copy B serves old text."""
    (a, b), store, _r = _two_copies(tmp_path)
    _call(b, "read_file", {"path": "main.tex"})

    async def nothing(project_id):
        return None
    monkeypatch.setattr(store, "get_head", nothing)
    _call(a, "edit_file", {"path": "main.tex", "old_string": "Words.", "new_string": "Changed by copy A."})
    _, seen = _call(b, "read_file", {"path": "main.tex"})
    assert "Words." in seen


# --- the worker itself, on a real Git remote ---------------------------------------------------------

def _cfg(remote: Path) -> ProjectConfig:
    return ProjectConfig(name="p", project_id=HEX, token="t", git_url=remote.as_uri())


def test_worker_refreshes_only_when_another_copy_pushed(tmp_path, monkeypatch):
    monkeypatch.setattr(git_worker, "MIN_PUSH_INTERVAL_SECONDS", 0.0)
    remote = _remote(tmp_path)
    store = InMemoryStore()
    w1, w2 = GitWorker(tmp_path / "w1", heads=store), GitWorker(tmp_path / "w2", heads=store)
    cfg = _cfg(remote)

    async def scenario():
        await w1.ensure_repo(cfg)
        await w2.ensure_repo(cfg)
        fetches = {"n": 0}
        real_fetch = w2._fetch

        async def counting_fetch(project, branch):
            fetches["n"] += 1
            return await real_fetch(project, branch)
        w2._fetch = counting_fetch

        # No push anywhere: inside the refresh window, copy 2 does not touch the remote.
        await w2.ensure_repo(cfg)
        assert fetches["n"] == 0

        # Copy 1 pushes; copy 2 must refresh on its next read.
        async with w1.lock_for(cfg):
            await w1.sync(cfg, force=True)
            (w1.repo_path(cfg) / "main.tex").write_text("pushed by copy 1\n")
            res = await w1.commit_and_push(cfg, "edit")
        assert res.pushed
        await w2.ensure_repo(cfg)
        assert fetches["n"] == 1
        assert (w2.repo_path(cfg) / "main.tex").read_text() == "pushed by copy 1\n"

        # Once caught up it goes quiet again.
        await w2.ensure_repo(cfg)
        assert fetches["n"] == 1

        # Someone edits in Overleaf directly (not through any copy). Copy 2 picks it up on the
        # normal refresh interval, and afterwards the older shared record must not make it fetch
        # on every read.
        seed = tmp_path / "seed"
        _git(["pull", "-q", "origin", "main"], seed)
        (seed / "main.tex").write_text("edited in Overleaf\n")
        _git(["-c", "user.name=S", "-c", "user.email=s@t", "commit", "-qam", "outside"], seed)
        _git(["push", "-q", "origin", "main"], seed)
        await w2.sync(cfg, force=True)
        n = fetches["n"]
        await w2.ensure_repo(cfg)
        await w2.ensure_repo(cfg)
        assert fetches["n"] == n
    asyncio.run(scenario())


def test_a_broken_shared_record_errs_on_the_side_of_refreshing(tmp_path):
    remote = _remote(tmp_path)

    class Broken:
        async def get_head(self, project_id):
            raise RuntimeError("storage down")

        async def put_head(self, project_id, sha):
            raise RuntimeError("storage down")

    w = GitWorker(tmp_path / "w", heads=Broken())
    cfg = _cfg(remote)

    async def scenario():
        await w.ensure_repo(cfg)
        assert await w._behind_another_copy(cfg) is True       # unknown means refresh, never serve stale
        async with w.lock_for(cfg):                            # and a push still succeeds without the record
            await w.sync(cfg, force=True)
            (w.repo_path(cfg) / "x.tex").write_text("x\n")
            res = await w.commit_and_push(cfg, "x")
        assert res.pushed
    asyncio.run(scenario())


def test_without_shared_storage_the_worker_behaves_as_before(tmp_path):
    remote = _remote(tmp_path)
    w = GitWorker(tmp_path / "w")
    cfg = _cfg(remote)

    async def scenario():
        await w.ensure_repo(cfg)
        assert await w._behind_another_copy(cfg) is False
    asyncio.run(scenario())


# --- 3. counting ------------------------------------------------------------------------------------

def test_concurrent_counting_loses_nothing_in_memory():
    store = InMemoryStore()

    async def go():
        await asyncio.gather(*(store.increment_usage("u", "2026-10") for _ in range(50)))
        return await store.get_usage("u", "2026-10")
    assert asyncio.run(go()) == 50
