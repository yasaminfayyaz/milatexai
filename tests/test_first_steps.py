"""The first project reply ends with ideas for what to ask, because many people connect and stop.

Shown once per account, only to someone with no commit yet this month, and never in the way of the
reply it rides on.
"""

from __future__ import annotations

import asyncio
import warnings

from fastmcp import Client

from leafbridge import hosted, web
from leafbridge.service import AccountService
from leafbridge.store import InMemoryStore, TokenCipher, User

warnings.filterwarnings("ignore", category=DeprecationWarning)
GOOD = "0123456789abcdef01234567"
MARK = "this is the first time this person has used MiLatexAI"


def _server(plan="free", used=0):
    store = InMemoryStore()
    cipher = TokenCipher(TokenCipher.generate_key())
    asyncio.run(store.upsert_user(User(user_id="u", email="u@x.com", plan=plan)))
    asyncio.run(AccountService(store, cipher).connect_project(
        "u", f"https://www.overleaf.com/project/{GOOD}", "olp_saved", "thesis"))
    if used:
        asyncio.run(store.increment_usage("u", hosted._month(), used))
    mcp = hosted.create_hosted_server(store=store, cipher=cipher, auth=False,
                                      identity_provider=lambda: ("u", "u@x.com"), base_url="https://milatexai.com")
    return mcp


def _call(mcp, *names):
    async def go():
        async with Client(mcp) as c:
            return [(await c.call_tool(n, {})).content[0].text for n in names]
    return asyncio.run(go())


def test_the_first_reply_carries_the_ideas_and_later_ones_do_not():
    first, second = _call(_server(), "list_projects", "list_projects")
    assert "thesis" in first and MARK in first
    assert "thesis" in second and MARK not in second


def test_free_accounts_are_told_the_figure_idea_is_pro_and_pro_accounts_are_not():
    free = _call(_server("free"), "list_projects")[0]
    pro = _call(_server("pro"), "list_projects")[0]
    assert "Figure Studio is a Pro feature" in free
    assert "matplotlib" in pro and "Figure Studio is a Pro feature" not in pro


def test_someone_who_already_commits_is_left_alone():
    assert MARK not in _call(_server(used=3), "list_projects")[0]


def test_the_web_page_shows_the_ideas():
    page = web.render_success("thesis", GOOD)
    assert all(p.split(" (Pro)")[0][:30] in page for p in web.FIRST_PROMPTS)
