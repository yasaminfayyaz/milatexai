"""When Pro ends: nothing is deleted, the free plan works on one active project at a time,
the user picks it (and can switch any time), and resubscribing brings every project back."""

from __future__ import annotations

import asyncio
import subprocess
import warnings
from urllib.parse import urlencode

from fastmcp import Client
from starlette.testclient import TestClient

from leafbridge.connect_link import mint_connect_code
from leafbridge.hosted import create_hosted_server
from leafbridge.service import AccountService
from leafbridge.store import InMemoryStore, TokenCipher, User

warnings.filterwarnings("ignore", category=DeprecationWarning)
IDS = {"thesis": "0123456789abcdef01234567", "paper": "fedcba9876543210fedcba98", "slides": "aaaaaaaaaaaaaaaaaaaaaaaa"}


def _git(args, cwd):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


def _remote(tmp_path, name):
    remote, seed = tmp_path / f"{name}.git", tmp_path / f"{name}-seed"
    remote.mkdir(); seed.mkdir()
    _git(["init", "--bare", "-b", "main", "."], remote)
    _git(["init", "-b", "main", "."], seed)
    (seed / "main.tex").write_text(f"% {name}\n")
    _git(["add", "-A"], seed)
    _git(["-c", "user.name=S", "-c", "user.email=s@t", "commit", "-qm", "init"], seed)
    _git(["push", "-q", remote.as_uri(), "main"], seed)
    return remote


def _world(tmp_path):
    store, cipher = InMemoryStore(), TokenCipher(TokenCipher.generate_key())
    svc = AccountService(store, cipher)
    asyncio.run(store.upsert_user(User(user_id="u", email="u@x.com", plan="pro")))
    first = True
    for name, pid in IDS.items():
        url = f"https://www.overleaf.com/project/{pid}"
        if first:
            asyncio.run(svc.connect_project("u", url, "olp_x", name, git_url=_remote(tmp_path, name).as_uri()))
            first = False
        else:
            asyncio.run(svc.add_project("u", url, name, git_url=_remote(tmp_path, name).as_uri()))
    mcp = create_hosted_server(store=store, cipher=cipher, auth=False, identity_provider=lambda: ("u", "u@x.com"),
                               base_url="https://milatexai.com", data_dir=tmp_path / "data")
    return mcp, store, cipher


def _plan(store, plan):
    user = asyncio.run(store.get_user("u"))
    user.plan = plan
    asyncio.run(store.upsert_user(user))


def _call(mcp, tool, args):
    async def go():
        async with Client(mcp) as c:
            r = await c.call_tool(tool, args, raise_on_error=False)
            return r.is_error, " ".join(getattr(x, "text", "") for x in r.content)
    return asyncio.run(go())


def test_on_pro_every_project_works(tmp_path):
    mcp, _, _ = _world(tmp_path)
    for name in IDS:
        is_error, text = _call(mcp, "read_file", {"path": "main.tex", "project": name})
        assert not is_error and f"% {name}" in text


def test_when_pro_ends_the_user_picks_one_project_and_the_rest_pause(tmp_path):
    mcp, store, _ = _world(tmp_path)
    _plan(store, "free")
    assert len(asyncio.run(store.list_projects("u"))) == 3                       # nothing deleted

    is_error, text = _call(mcp, "read_file", {"path": "main.tex", "project": "paper"})
    assert not is_error                                                           # a question, not a failure
    assert "one project at a time" in text and all(n in text for n in IDS)

    is_error, text = _call(mcp, "switch_project", {"project": "paper"})
    assert not is_error and "'paper' is now your active project" in text and "2 project(s) are paused" in text

    is_error, text = _call(mcp, "read_file", {"path": "main.tex"})                 # no name needed now
    assert not is_error and "% paper" in text
    is_error, text = _call(mcp, "read_file", {"path": "main.tex", "project": "thesis"})
    assert not is_error and "'thesis' is paused" in text and "% thesis" not in text

    is_error, text = _call(mcp, "list_projects", {})
    assert "paper" in text and "[active]" in text and text.count("[paused]") == 2

    _call(mcp, "switch_project", {"project": "thesis"})                           # switch any time
    is_error, text = _call(mcp, "read_file", {"path": "main.tex"})
    assert "% thesis" in text
    is_error, text = _call(mcp, "edit_file", {"path": "main.tex", "project": "paper",
                                              "old_string": "% paper", "new_string": "% changed"})
    assert not is_error and "is paused" in text                                   # writes are paused too


def test_resubscribing_brings_every_project_back(tmp_path):
    mcp, store, _ = _world(tmp_path)
    _plan(store, "free")
    _call(mcp, "switch_project", {"project": "paper"})
    _plan(store, "pro")
    for name in IDS:
        assert f"% {name}" in _call(mcp, "read_file", {"path": "main.tex", "project": name})[1]
    assert "[paused]" not in _call(mcp, "list_projects", {})[1]


def test_a_free_user_with_one_project_never_sees_any_of_this(tmp_path):
    store, cipher = InMemoryStore(), TokenCipher(TokenCipher.generate_key())
    asyncio.run(store.upsert_user(User(user_id="u", email="u@x.com")))
    asyncio.run(AccountService(store, cipher).connect_project(
        "u", f"https://www.overleaf.com/project/{IDS['thesis']}", "olp_x", "thesis",
        git_url=_remote(tmp_path, "thesis").as_uri()))
    mcp = create_hosted_server(store=store, cipher=cipher, auth=False, identity_provider=lambda: ("u", "u@x.com"),
                               base_url="https://milatexai.com", data_dir=tmp_path / "data")
    assert "% thesis" in _call(mcp, "read_file", {"path": "main.tex"})[1]
    assert "[" not in _call(mcp, "list_projects", {})[1]


def test_the_projects_page_shows_paused_projects_and_switches(tmp_path):
    mcp, store, cipher = _world(tmp_path)
    _plan(store, "free")
    code = mint_connect_code(cipher, "u", "u@x.com")
    with TestClient(mcp.http_app()) as client:
        page = client.get(f"/projects?code={code}").text
        assert "one project at a time" in page and page.count("· Paused") == 3 and "Make active" in page
        body = urlencode({"code": code, "action": "activate", "project_id": IDS["slides"]})
        page = client.post("/projects", content=body,
                           headers={"content-type": "application/x-www-form-urlencoded"}).text
    assert page.count("· Paused") == 2 and "· Active" in page
    assert asyncio.run(store.get_user("u")).active_project_id == IDS["slides"]
