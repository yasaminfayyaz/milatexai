"""add_project checks that the link and token really open the repository.

Before, a made-up Overleaf id (000000000000000000000000) was accepted with "You can now
edit it." and only failed on first use with a raw git error. Now one light call
(``git ls-remote --heads``) decides: clearly refused means nothing is added and the user is
told why; a busy or unreachable host keeps the project, with a note.
"""

from __future__ import annotations

import asyncio
import subprocess
import warnings

import pytest
from fastmcp import Client

from leafbridge import hosted
from leafbridge.config import ProjectConfig
from leafbridge.git_worker import GitError, GitWorker
from leafbridge.service import AccountService
from leafbridge.store import InMemoryStore, TokenCipher, User

warnings.filterwarnings("ignore", category=DeprecationWarning)
GOOD = "0123456789abcdef01234567"


def _cfg(url: str) -> ProjectConfig:
    return ProjectConfig(name="p", project_id=GOOD, token="olp_testtoken_0123456789", git_url=url)


def test_a_repository_that_exists_is_reachable_and_a_missing_one_is_refused(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", "."], cwd=remote, check=True)
    worker = GitWorker(tmp_path / "w")
    assert asyncio.run(worker.can_reach(_cfg(remote.as_uri()))) == "ok"
    assert asyncio.run(worker.can_reach(_cfg((tmp_path / "nope.git").as_uri()))) == "denied"


@pytest.mark.parametrize("message, verdict", [
    ("fatal: unable to access 'https://git.overleaf.com/x/': The requested URL returned error: 403", "denied"),
    ("remote: Repository not found.", "denied"),
    ("fatal: Authentication failed for 'https://git.overleaf.com/x/'", "denied"),
    ("fatal: unable to access 'https://git.overleaf.com/x/': Could not resolve host: git.overleaf.com", "unknown"),
    ("error: RPC failed; HTTP 429 Too Many Requests", "unknown"),
    ("something nobody expected", "unknown"),
])
def test_refusals_and_temporary_problems_are_told_apart(tmp_path, monkeypatch, message, verdict):
    worker = GitWorker(tmp_path / "w")

    async def fake_git(project, args, **kw):
        raise GitError(message)
    monkeypatch.setattr(worker, "_git", fake_git)
    assert asyncio.run(worker.can_reach(_cfg("https://git.overleaf.com/x"))) == verdict


def _server(monkeypatch, verdict):
    store = InMemoryStore()
    cipher = TokenCipher(TokenCipher.generate_key())
    asyncio.run(store.upsert_user(User(user_id="u", email="u@x.com", plan="pro")))
    asyncio.run(AccountService(store, cipher).connect_project(
        "u", f"https://www.overleaf.com/project/{GOOD}", "olp_saved", "thesis"))

    async def fake_reach(self, project):
        return verdict
    monkeypatch.setattr(GitWorker, "can_reach", fake_reach)
    mcp = hosted.create_hosted_server(store=store, cipher=cipher, auth=False,
                                      identity_provider=lambda: ("u", "u@x.com"), base_url="https://milatexai.com")
    return mcp, store


def _add(mcp, url):
    async def go():
        async with Client(mcp) as c:
            r = await c.call_tool("add_project", {"overleaf_url": url}, raise_on_error=False)
            return r.is_error, " ".join(getattr(b, "text", "") for b in r.content)
    return asyncio.run(go())


def test_a_project_the_token_cannot_open_is_not_added(monkeypatch):
    mcp, store = _server(monkeypatch, "denied")
    err, out = _add(mcp, "https://www.overleaf.com/project/000000000000000000000000")
    assert err and "was not added" in out and "You can now edit it" not in out
    assert [p.project_id for p in asyncio.run(store.list_projects("u"))] == [GOOD]


def test_a_project_the_token_opens_is_added(monkeypatch):
    mcp, store = _server(monkeypatch, "ok")
    err, out = _add(mcp, "https://www.overleaf.com/project/111111111111111111111111")
    assert not err and "You can now edit it." in out and "couldn't confirm" not in out
    assert len(asyncio.run(store.list_projects("u"))) == 2


def test_when_the_host_is_busy_the_project_is_kept_with_a_note(monkeypatch):
    mcp, store = _server(monkeypatch, "unknown")
    err, out = _add(mcp, "https://www.overleaf.com/project/222222222222222222222222")
    assert not err and "couldn't confirm access right now" in out
    assert len(asyncio.run(store.list_projects("u"))) == 2
