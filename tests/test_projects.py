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
from leafbridge.service import AccountService, ProjectNotConnected, ServiceError
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
