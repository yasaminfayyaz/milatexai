"""Tests for the Phase 2 multi-tenant store + token encryption."""

from __future__ import annotations

import pytest

from leafbridge.store import (
    FREE_MONTHLY_COMMITS,
    FREE_PROJECT_LIMIT,
    InMemoryStore,
    Project,
    TokenCipher,
    TokenDecryptError,
    User,
    monthly_commit_limit,
    project_limit,
)


def test_token_cipher_roundtrip():
    cipher = TokenCipher(TokenCipher.generate_key())
    secret = "olp_redacted_test_token_not_real"
    enc = cipher.encrypt(secret)
    assert enc != secret  # actually encrypted
    assert secret not in enc
    assert cipher.decrypt(enc) == secret


def test_token_cipher_wrong_key_fails():
    a = TokenCipher(TokenCipher.generate_key())
    b = TokenCipher(TokenCipher.generate_key())
    enc = a.encrypt("secret-token")
    with pytest.raises(TokenDecryptError):
        b.decrypt(enc)


def test_from_env_ephemeral_flag(monkeypatch):
    monkeypatch.delenv("LEAFBRIDGE_ENC_KEY", raising=False)
    _, ephemeral = TokenCipher.from_env()
    assert ephemeral is True
    monkeypatch.setenv("LEAFBRIDGE_ENC_KEY", TokenCipher.generate_key())
    _, ephemeral = TokenCipher.from_env()
    assert ephemeral is False


async def test_user_crud():
    store = InMemoryStore()
    assert await store.get_user("u1") is None
    await store.upsert_user(User(user_id="u1", email="a@b.com"))
    u = await store.get_user("u1")
    assert u is not None and u.email == "a@b.com" and u.plan == "free"


async def test_project_crud_and_isolation():
    store = InMemoryStore()
    cipher = TokenCipher(TokenCipher.generate_key())
    await store.put_project(
        Project("u1", "a" * 24, "thesis", cipher.encrypt("tok-1"))
    )
    await store.put_project(
        Project("u2", "b" * 24, "other", cipher.encrypt("tok-2"))
    )
    # Users only see their own projects.
    mine = await store.list_projects("u1")
    assert len(mine) == 1 and mine[0].name == "thesis"
    # Stored token is ciphertext, decryptable back to the original.
    assert mine[0].token_encrypted != "tok-1"
    assert cipher.decrypt(mine[0].token_encrypted) == "tok-1"
    # Delete is scoped to the owner.
    assert await store.delete_project("u1", "a" * 24) is True
    assert await store.delete_project("u1", "a" * 24) is False
    assert len(await store.list_projects("u2")) == 1


async def test_usage_counter():
    store = InMemoryStore()
    assert await store.get_usage("u1", "2026-07") == 0
    assert await store.increment_usage("u1", "2026-07") == 1
    assert await store.increment_usage("u1", "2026-07", by=4) == 5
    assert await store.get_usage("u1", "2026-07") == 5
    # Different month is independent.
    assert await store.get_usage("u1", "2026-08") == 0


def test_plan_limits():
    free = User("u", "a@b.com", plan="free")
    pro = User("u", "a@b.com", plan="pro")
    admin = User("u", "a@b.com", is_admin=True)
    assert monthly_commit_limit(free) == FREE_MONTHLY_COMMITS
    assert project_limit(free) == FREE_PROJECT_LIMIT
    assert monthly_commit_limit(pro) is None  # unlimited
    assert project_limit(pro) is None
    assert monthly_commit_limit(admin) is None  # admin bypass
