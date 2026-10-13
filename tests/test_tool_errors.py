"""Tool error results are for real failures only (the directory reports them as an error rate),
and the common mistakes come back with enough detail to get the next call right."""

from __future__ import annotations

import asyncio
import subprocess
import warnings

from fastmcp import Client

from leafbridge.hosted import _near_miss, create_hosted_server
from leafbridge.service import AccountService
from leafbridge.store import InMemoryStore, TokenCipher, User

warnings.filterwarnings("ignore", category=DeprecationWarning)
PID = "0123456789abcdef01234567"
INTRO = "\\section{Introduction}\nWe study tables.\nThey are useful.\n"


def _git(args, cwd):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


def _server(tmp_path, *, connected=True):
    store, cipher = InMemoryStore(), TokenCipher(TokenCipher.generate_key())
    asyncio.run(store.upsert_user(User(user_id="u", email="u@x.com", plan="pro")))
    if connected:
        remote, seed = tmp_path / "r.git", tmp_path / "seed"
        remote.mkdir(); seed.mkdir()
        _git(["init", "--bare", "-b", "main", "."], remote)
        _git(["init", "-b", "main", "."], seed)
        (seed / "chapters").mkdir()
        (seed / "chapters" / "intro.tex").write_text(INTRO)
        (seed / "main.tex").write_text("\\documentclass{article}\\begin{document}\\input{chapters/intro}\\end{document}\n")
        _git(["add", "-A"], seed)
        _git(["-c", "user.name=S", "-c", "user.email=s@t", "commit", "-qm", "init"], seed)
        _git(["push", "-q", remote.as_uri(), "main"], seed)
        asyncio.run(AccountService(store, cipher).connect_project(
            "u", f"https://www.overleaf.com/project/{PID}", "olp_x", "paper", git_url=remote.as_uri()))
    return create_hosted_server(store=store, cipher=cipher, auth=False, identity_provider=lambda: ("u", "u@x.com"),
                                base_url="https://milatexai.com", data_dir=tmp_path / "data")


def _call(mcp, tool, args):
    async def go():
        async with Client(mcp) as c:
            r = await c.call_tool(tool, args, raise_on_error=False)
            return r.is_error, " ".join(getattr(x, "text", "") for x in r.content)
    return asyncio.run(go())


def test_a_new_users_first_call_returns_the_connect_link_as_a_normal_reply(tmp_path):
    is_error, text = _call(_server(tmp_path, connected=False), "list_files", {})
    assert not is_error
    assert "https://milatexai.com/connect?code=" in text
    is_error, text = _call(_server(tmp_path / "img", connected=False), "show_page", {})   # an image tool too
    assert not is_error and "/connect?code=" in text


def test_a_wrong_file_name_suggests_the_right_one(tmp_path):
    is_error, text = _call(_server(tmp_path), "read_file", {"path": "intro.tex"})
    assert is_error and "Did you mean: chapters/intro.tex?" in text


def test_a_failed_edit_says_why(tmp_path):
    mcp = _server(tmp_path)
    is_error, text = _call(mcp, "edit_file", {"path": "chapters/intro.tex",
                                              "old_string": "2\tWe study tables.", "new_string": "We study floats."})
    assert is_error and "line numbers" in text
    is_error, text = _call(mcp, "edit_file", {"path": "chapters/intro.tex",
                                              "old_string": "We study  tables.\nThey  are useful.", "new_string": "x"})
    assert is_error and "spacing" in text


def test_near_miss_points_at_the_closest_line():
    assert "Closest line is 2" in _near_miss(INTRO, "We study tabels.")
    assert _near_miss(INTRO, "nothing like it at all, completely different") == ""
