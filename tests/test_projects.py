"""Tests for the account-level token + the three flows: onboarding (token +
project), managing the project list (add/remove, no token), and change/revoke
token."""

from __future__ import annotations

import asyncio
import warnings

import pytest
from starlette.testclient import TestClient

from leafbridge.capacity import CapacityGate
from leafbridge.connect_link import mint_connect_code
from leafbridge.hosted import create_hosted_server
from leafbridge.hosted import HostedApp
from leafbridge.service import (
    AccountService,
    LimitExceeded,
    ProjectNotConnected,
    ServiceError,
)
from leafbridge.store import InMemoryStore, Project, TokenCipher

warnings.filterwarnings("ignore", category=DeprecationWarning)

HEX1 = "0123456789abcdef01234567"
HEX2 = "1123456789abcdef01234567"
URL1 = f"https://www.overleaf.com/project/{HEX1}"
URL2 = f"https://www.overleaf.com/project/{HEX2}"


def _svc() -> AccountService:
    return AccountService(InMemoryStore(), TokenCipher(TokenCipher.generate_key()))


def _admin_user(svc: AccountService, uid="u", email="e@x.com"):
    return asyncio.run(svc.get_or_create_user(uid, email, admin_emails=(email,)))


# --- service: account-level token ------------------------------------------

def test_add_project_reuses_account_token_no_token_needed():
    svc = _svc()
    _admin_user(svc)
    asyncio.run(svc.connect_project("u", URL1, "olp_tok", "first"))
    # Add a SECOND project with only a URL — no token argument at all.
    p2 = asyncio.run(svc.add_project("u", URL2, "second"))
    assert p2.project_id == HEX2
    assert p2.token_encrypted == ""  # relies on the account token
    # Both projects resolve to git configs using the same account token.
    assert asyncio.run(svc.resolve_project("u", "first")).token == "olp_tok"
    assert asyncio.run(svc.resolve_project("u", "second")).token == "olp_tok"


def test_add_project_before_any_token_errors():
    svc = _svc()
    _admin_user(svc)
    with pytest.raises(ServiceError):
        asyncio.run(svc.add_project("u", URL1, "x"))


def test_set_token_updates_all_projects():
    svc = _svc()
    _admin_user(svc)
    asyncio.run(svc.connect_project("u", URL1, "olp_old", "first"))
    asyncio.run(svc.add_project("u", URL2, "second"))
    asyncio.run(svc.set_token("u", "olp_new"))
    assert asyncio.run(svc.resolve_project("u", "first")).token == "olp_new"
    assert asyncio.run(svc.resolve_project("u", "second")).token == "olp_new"


def test_revoke_token_blocks_access_until_re_added():
    svc = _svc()
    _admin_user(svc)
    asyncio.run(svc.connect_project("u", URL1, "olp_tok", "first"))
    asyncio.run(svc.revoke_token("u"))
    with pytest.raises(ProjectNotConnected):
        asyncio.run(svc.resolve_project("u", "first"))  # project stays, token gone
    asyncio.run(svc.set_token("u", "olp_again"))
    assert asyncio.run(svc.resolve_project("u", "first")).token == "olp_again"


def test_get_or_create_promotes_admin_and_fills_email_on_later_login():
    svc = _svc()
    # First seen with NO email (token lacked the claim) -> not admin.
    asyncio.run(svc.get_or_create_user("u", "", admin_emails=("boss@x.com",)))
    u = asyncio.run(svc.store.get_user("u"))
    assert u.is_admin is False and u.email == ""
    # A later login supplies the admin email -> promoted + email backfilled.
    u2 = asyncio.run(svc.get_or_create_user("u", "boss@x.com", admin_emails=("boss@x.com",)))
    assert u2.is_admin is True and u2.email == "boss@x.com"


def test_free_project_limit_message_mentions_swap_and_upgrade():
    svc = _svc()
    asyncio.run(svc.get_or_create_user("u", "u@x.com"))  # free (not admin)
    asyncio.run(svc.connect_project("u", URL1, "olp_tok", "first"))
    with pytest.raises(LimitExceeded) as ei:
        asyncio.run(svc.add_project("u", URL2, "second"))
    msg = str(ei.value).lower()
    assert "remove" in msg and "upgrade" in msg


def test_user_email_backfilled_from_resolver_promotes_admin(tmp_path):
    store = InMemoryStore()
    cipher = TokenCipher(TokenCipher.generate_key())

    async def resolver(_uid: str) -> str:
        return "boss@x.com"

    app = HostedApp(
        store=store, cipher=cipher, data_dir=tmp_path,
        admin_emails=("boss@x.com",),
        identity_provider=lambda: ("u", ""),  # token carries no email
        email_resolver=resolver,
    )
    u = asyncio.run(app.user())
    assert u.email == "boss@x.com" and u.is_admin is True


def test_legacy_per_project_token_still_resolves_and_backfills():
    # A project connected before account tokens existed (token on the project).
    svc = _svc()
    _admin_user(svc)
    asyncio.run(svc.store.put_project(Project(
        user_id="u", project_id=HEX1, name="legacy",
        token_encrypted=svc.cipher.encrypt("olp_legacy"))))
    assert asyncio.run(svc.resolve_project("u", "legacy")).token == "olp_legacy"
    # add_project backfills the account token from the legacy project, so it works.
    asyncio.run(svc.add_project("u", URL2, "second"))
    assert asyncio.run(svc.resolve_project("u", "second")).token == "olp_legacy"


# --- service: per-project token update -------------------------------------

GH_URL = "https://github.com/owner/repo"


def test_update_project_token_replaces_repo_token_and_leaves_account_alone():
    svc = _svc()
    _admin_user(svc)
    # Overleaf project holds the account token; a GitHub repo holds its own.
    asyncio.run(svc.connect_project("u", URL1, "olp_tok", "thesis"))
    asyncio.run(svc.connect_project("u", GH_URL, "ghp_old", "gh"))
    acct_before = asyncio.run(svc.store.get_user("u")).overleaf_token_encrypted
    proj = asyncio.run(svc.update_project_token("u", "gh", "ghp_new"))
    assert svc.cipher.decrypt(proj.token_encrypted) == "ghp_new"
    # The new token takes effect immediately via resolve_project.authed_url().
    cfg = asyncio.run(svc.resolve_project("u", "gh"))
    assert cfg.authed_url() == "https://x-access-token:ghp_new@github.com/owner/repo.git"
    # The account-level Overleaf token is untouched.
    assert asyncio.run(svc.store.get_user("u")).overleaf_token_encrypted == acct_before
    assert asyncio.run(svc.resolve_project("u", "thesis")).token == "olp_tok"


def test_update_project_token_rejects_empty_and_paste():
    svc = _svc()
    _admin_user(svc)
    asyncio.run(svc.connect_project("u", GH_URL, "ghp_old", "gh"))
    with pytest.raises(ServiceError):
        asyncio.run(svc.update_project_token("u", "gh", ""))
    with pytest.raises(ServiceError):
        asyncio.run(svc.update_project_token("u", "gh", "PASTE_your_token_here"))


def test_update_project_token_foreign_ref_is_out_of_scope():
    svc = _svc()
    _admin_user(svc)
    asyncio.run(svc.connect_project("u", URL1, "olp_tok", "first"))
    # A project the user does not have -> ProjectNotConnected (auth scope).
    with pytest.raises(ProjectNotConnected):
        asyncio.run(svc.update_project_token("u", "nope", "ghp_new"))


# --- web routes ------------------------------------------------------------

def _server():
    store = InMemoryStore()
    cipher = TokenCipher(TokenCipher.generate_key())
    mcp = create_hosted_server(
        store=store, cipher=cipher, auth=False,
        identity_provider=lambda: ("u", "e@x.com"),
        base_url="https://milatexai.com", admin_emails=("e@x.com",),
        capacity=CapacityGate(subscription_id="", resource_group="", stripe_api_key=""),
    )
    return store, cipher, mcp


def test_manage_projects_and_token_routes_end_to_end():
    store, cipher, mcp = _server()
    code = mint_connect_code(cipher, "u", "e@x.com")
    with TestClient(mcp.http_app()) as client:
        # onboard (token + first project)
        assert client.post("/connect", data={
            "code": code, "overleaf_url": URL1, "token": "olp_tok", "name": "first",
        }).status_code == 200

        # manage page lists the project
        g = client.get("/projects", params={"code": code})
        assert g.status_code == 200 and "first" in g.text

        # add a second project with NO token
        a = client.post("/projects", data={
            "code": code, "action": "add", "overleaf_url": URL2, "name": "second"})
        assert a.status_code == 200 and "second" in a.text
        assert len(asyncio.run(store.list_projects("u"))) == 2

        # remove the first
        assert client.post("/projects", data={
            "code": code, "action": "remove", "project_id": HEX1}).status_code == 200
        remaining = asyncio.run(store.list_projects("u"))
        assert [p.project_id for p in remaining] == [HEX2]

        # token page renders, then revoke
        assert client.get("/token", params={"code": code}).status_code == 200
        assert client.post("/token", data={"code": code, "action": "revoke"}).status_code == 200
    assert asyncio.run(store.get_user("u")).overleaf_token_encrypted == ""


def test_projects_route_set_token_updates_and_escapes():
    store, cipher, mcp = _server()
    code = mint_connect_code(cipher, "u", "e@x.com")
    with TestClient(mcp.http_app()) as client:
        # Onboard an Overleaf account token, then connect a GitHub repo.
        client.post("/connect", data={
            "code": code, "overleaf_url": URL1, "token": "olp_tok", "name": "first"})
        client.post("/projects", data={
            "code": code, "action": "add",
            "overleaf_url": "https://github.com/owner/repo",
            "token": "ghp_old", "name": "gh"})
        gh = next(p for p in asyncio.run(store.list_projects("u")) if p.name == "gh")

        # Update just that repo's token, no URL re-entry.
        r = client.post("/projects", data={
            "code": code, "action": "set_token",
            "project_id": gh.project_id, "token": "ghp_new"})
        assert r.status_code == 200
        updated = next(p for p in asyncio.run(store.list_projects("u")) if p.name == "gh")
        assert cipher.decrypt(updated.token_encrypted) == "ghp_new"
        # The account token is left alone.
        assert cipher.decrypt(asyncio.run(store.get_user("u")).overleaf_token_encrypted) == "olp_tok"
        # The submitted secret is never echoed back into the page.
        assert "ghp_new" not in r.text
        # Interpolated values are HTML-escaped: no raw injection breaks out.
        assert "<script>" not in r.text


def test_token_form_never_echoes_token():
    store, cipher, mcp = _server()
    code = mint_connect_code(cipher, "u", "e@x.com")
    with TestClient(mcp.http_app()) as client:
        client.post("/connect", data={
            "code": code, "overleaf_url": URL1, "token": "olp_tok", "name": "first"})
        # A rejected token (contains PASTE) re-renders the form — the submitted
        # value must never be echoed back into the HTML.
        r = client.post("/token", data={
            "code": code, "action": "set", "token": "PASTE_secret_xyz"})
    assert "PASTE_secret_xyz" not in r.text
