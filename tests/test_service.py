"""Tests for the Phase 2 hosted business logic (AccountService)."""

from __future__ import annotations

import pytest

from leafbridge.service import (
    AccountService,
    LimitExceeded,
    ProjectNotConnected,
    ServiceError,
)
from leafbridge.store import InMemoryStore, TokenCipher, User

HEX1 = "0123456789abcdef01234567"
HEX2 = "89abcdef0123456789abcdef"
TOKEN = "olp_secret_token_value"


def make_service() -> AccountService:
    return AccountService(InMemoryStore(), TokenCipher(TokenCipher.generate_key()))


async def test_get_or_create_user_and_admin():
    svc = make_service()
    u = await svc.get_or_create_user("u1", "a@b.com")
    assert u.plan == "free" and not u.is_admin
    # idempotent
    assert (await svc.get_or_create_user("u1", "a@b.com")).user_id == "u1"
    admin = await svc.get_or_create_user("u2", "me@milatexai.com",
                                         admin_emails=("me@milatexai.com",))
    assert admin.is_admin


async def test_connect_encrypts_and_enforces_free_project_limit():
    svc = make_service()
    await svc.get_or_create_user("u1", "a@b.com")
    p = await svc.connect_project("u1", f"https://www.overleaf.com/project/{HEX1}", TOKEN, "thesis")
    assert p.project_id == HEX1
    assert p.token_encrypted != TOKEN and TOKEN not in p.token_encrypted  # encrypted

    # Free plan = 1 project: a 2nd distinct project is refused...
    with pytest.raises(LimitExceeded):
        await svc.connect_project("u1", HEX2, TOKEN)
    # ...but re-connecting the SAME project (token update) is allowed.
    await svc.connect_project("u1", HEX1, "olp_new_token", "thesis")
    assert len(await svc.store.list_projects("u1")) == 1


async def test_pro_user_unlimited_projects():
    svc = make_service()
    await svc.store.upsert_user(User("u1", "a@b.com", plan="pro"))
    await svc.connect_project("u1", HEX1, TOKEN)
    await svc.connect_project("u1", HEX2, TOKEN)  # no LimitExceeded
    assert len(await svc.store.list_projects("u1")) == 2


async def test_connect_rejects_bad_input():
    svc = make_service()
    await svc.get_or_create_user("u1", "a@b.com")
    with pytest.raises(ServiceError):
        await svc.connect_project("u1", HEX1, "PASTE_YOUR_TOKEN")  # placeholder token
    with pytest.raises(ServiceError):
        await svc.connect_project("u1", "not-a-project-url", TOKEN)


async def test_resolve_decrypts_to_projectconfig():
    svc = make_service()
    await svc.get_or_create_user("u1", "a@b.com")
    await svc.connect_project("u1", HEX1, TOKEN, "thesis")
    cfg = await svc.resolve_project("u1")  # single project -> default
    assert cfg.project_id == HEX1
    assert cfg.token == TOKEN  # decrypted back to the original
    assert cfg.clone_url == f"https://git.overleaf.com/{HEX1}"


async def test_resolve_errors():
    svc = make_service()
    await svc.get_or_create_user("u1", "a@b.com")
    with pytest.raises(ProjectNotConnected):
        await svc.resolve_project("u1")  # nothing connected
    await svc.store.upsert_user(User("u2", "b@b.com", plan="pro"))
    await svc.connect_project("u2", HEX1, TOKEN, "a")
    await svc.connect_project("u2", HEX2, TOKEN, "b")
    with pytest.raises(ProjectNotConnected):
        await svc.resolve_project("u2")  # ambiguous, needs a ref
    assert (await svc.resolve_project("u2", "b")).project_id == HEX2


async def test_commit_metering_free_limit_and_reset():
    svc = make_service()
    await svc.get_or_create_user("u1", "a@b.com")
    for _ in range(25):
        await svc.check_commit_allowed("u1", "2026-07")
        await svc.record_commit("u1", "2026-07")
    with pytest.raises(LimitExceeded):
        await svc.check_commit_allowed("u1", "2026-07")  # 26th blocked
    # New month resets.
    await svc.check_commit_allowed("u1", "2026-08")


async def test_commit_metering_pro_and_admin_unlimited():
    svc = make_service()
    await svc.store.upsert_user(User("pro", "p@b.com", plan="pro"))
    await svc.store.upsert_user(User("adm", "a@b.com", is_admin=True))
    for uid in ("pro", "adm"):
        for _ in range(100):
            await svc.check_commit_allowed(uid, "2026-07")
            await svc.record_commit(uid, "2026-07")  # never raises
