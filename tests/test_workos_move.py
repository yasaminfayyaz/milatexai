"""Moving sign-in from the WorkOS staging environment to production without disrupting anyone.

The promises:
- Connections made through either environment keep working, each token checked against
  its OWN environment's keys, issuer and client id.
- Someone signing in through production lands in their existing account (matched once by
  verified email), so projects, Pro and the Stripe subscription carry over untouched.
- An unverified or unknown email never joins two accounts.
"""

from __future__ import annotations

import asyncio
import warnings

import pytest
from fastmcp import Client
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from starlette.testclient import TestClient

from leafbridge import hosted
from leafbridge.authkit import MiLatexAIAuthKit
from leafbridge.service import AccountService
from leafbridge.store import InMemoryStore, Project, TokenCipher, User

warnings.filterwarnings("ignore", category=DeprecationWarning)

STAGING, PROD = "https://staging-env.authkit.app", "https://prod-env.authkit.app"
BASE, RESOURCE = "https://milatexai.com", "https://milatexai.com/mcp"
OLD_ID, NEW_ID = "user_01STAGINGID", "user_01PRODUCTIONID"


# --- tokens from both environments, each checked against its own environment ----------------

@pytest.fixture(scope="module")
def keys():
    return {"staging": RSAKeyPair.generate(), "prod": RSAKeyPair.generate()}


def _provider(keys, primary="staging"):
    main, other = (STAGING, PROD) if primary == "staging" else (PROD, STAGING)
    ids = {STAGING: "client_staging", PROD: "client_prod"}
    p = MiLatexAIAuthKit(authkit_domain=main, base_url=BASE, extra_audiences=[ids[main]],
                         also_trust=[(other, ids[other])])
    pem = {STAGING: keys["staging"].public_key, PROD: keys["prod"].public_key}
    # Local keys instead of fetching each environment's published ones.
    p.token_verifier = JWTVerifier(public_key=pem[main], issuer=main, algorithm="RS256")
    p._other_verifiers = [JWTVerifier(public_key=pem[other], issuer=other, algorithm="RS256")]
    p.set_mcp_path("/mcp")
    return p


def _ok(p, token):
    return asyncio.run(p.verify_token(token)) is not None


@pytest.mark.parametrize("primary", ["staging", "production"])
def test_tokens_from_both_environments_are_accepted_before_and_after_the_switch(keys, primary):
    p = _provider(keys, primary)
    assert _ok(p, keys["staging"].create_token(subject=OLD_ID, issuer=STAGING, audience=RESOURCE))
    assert _ok(p, keys["prod"].create_token(subject=NEW_ID, issuer=PROD, audience=RESOURCE))
    # Each environment's own client id also counts as "for this server" (see authkit.py).
    assert _ok(p, keys["staging"].create_token(subject=OLD_ID, issuer=STAGING, audience="client_staging"))
    assert _ok(p, keys["prod"].create_token(subject=NEW_ID, issuer=PROD, audience="client_prod"))


def test_each_environment_is_held_to_its_own_keys_issuer_and_client(keys):
    p = _provider(keys)
    # Signed with the wrong environment's key.
    assert not _ok(p, keys["staging"].create_token(subject=NEW_ID, issuer=PROD, audience=RESOURCE))
    assert not _ok(p, keys["prod"].create_token(subject=OLD_ID, issuer=STAGING, audience=RESOURCE))
    # The other environment's client id is not a valid audience here.
    assert not _ok(p, keys["prod"].create_token(subject=NEW_ID, issuer=PROD, audience="client_staging"))
    # Some other audience, some other issuer, or expired: refused.
    assert not _ok(p, keys["prod"].create_token(subject=NEW_ID, issuer=PROD, audience="https://evil.example/mcp"))
    assert not _ok(p, keys["prod"].create_token(subject=NEW_ID, issuer="https://evil.authkit.app", audience=RESOURCE))
    assert not _ok(p, keys["prod"].create_token(subject=NEW_ID, issuer=PROD, audience=RESOURCE, expires_in_seconds=-60))


def test_new_sign_ins_are_sent_to_the_environment_chosen(keys):
    assert [str(a).rstrip("/") for a in _provider(keys, "staging").authorization_servers] == [STAGING]
    assert [str(a).rstrip("/") for a in _provider(keys, "production").authorization_servers] == [PROD]


# --- accounts carry over -----------------------------------------------------------------------

def _store_with_existing_account(**kw):
    store = InMemoryStore()
    asyncio.run(store.upsert_user(User(user_id=OLD_ID, email="Ada@Example.com", plan="pro",
                                       stripe_customer_id="cus_123", **kw)))
    asyncio.run(store.put_project(Project(user_id=OLD_ID, project_id="0123456789abcdef01234567", name="Thesis")))
    return store


def _service(store):
    return AccountService(store, TokenCipher(TokenCipher.generate_key()))


def test_a_verified_email_lands_in_the_existing_account_and_is_remembered():
    store = _store_with_existing_account()
    svc = _service(store)
    assert asyncio.run(svc.link_identity(NEW_ID, "ada@example.com", True)) == OLD_ID
    # Remembered: later answers do not depend on the email any more.
    assert asyncio.run(svc.link_identity(NEW_ID, "", False)) == OLD_ID


def test_an_unverified_or_unknown_email_never_joins_two_accounts():
    store = _store_with_existing_account()
    svc = _service(store)
    assert asyncio.run(svc.link_identity(NEW_ID, "ada@example.com", False)) == NEW_ID
    assert asyncio.run(svc.link_identity("user_01SOMEONEELSE", "bob@example.com", True)) == "user_01SOMEONEELSE"


def test_with_two_accounts_on_one_email_the_paying_one_wins():
    store = _store_with_existing_account()
    asyncio.run(store.upsert_user(User(user_id="user_01AAAOTHER", email="ada@example.com")))
    assert asyncio.run(_service(store).link_identity(NEW_ID, "ada@example.com", True)) == OLD_ID


def _server(store, monkeypatch, identity, *, lookup=("ada@example.com", True), signin="staging", web_auth=None):
    monkeypatch.setenv("WORKOS_AUTHKIT_DOMAIN", STAGING)
    monkeypatch.setenv("WORKOS_PROD_AUTHKIT_DOMAIN", PROD)
    monkeypatch.setenv("WORKOS_PROD_API_KEY", "sk_test_prod")
    monkeypatch.setenv("WORKOS_PROD_CLIENT_ID", "client_prod")
    monkeypatch.setenv("WORKOS_SIGNIN", signin)
    calls = []

    def fake_lookup(api_key):
        async def look(user_id):
            calls.append(user_id)
            return lookup
        return look
    monkeypatch.setattr(hosted, "workos_user_lookup", fake_lookup)
    mcp = hosted.create_hosted_server(store=store, cipher=TokenCipher(TokenCipher.generate_key()), auth=False,
                                      identity_provider=lambda: identity, base_url=BASE, web_auth=web_auth)
    return mcp, calls


def _call(mcp, tool, args=None):
    async def go():
        async with Client(mcp) as c:
            r = await c.call_tool(tool, args or {}, raise_on_error=False)
            return " ".join(getattr(b, "text", "") for b in r.content)
    return asyncio.run(go())


def test_signing_in_through_production_keeps_projects_pro_and_the_subscription(monkeypatch):
    store = _store_with_existing_account()
    mcp, calls = _server(store, monkeypatch, (NEW_ID, "", PROD))
    assert "Thesis" in _call(mcp, "list_projects")
    assert "already on Pro" in _call(mcp, "upgrade")
    assert asyncio.run(store.get_user(NEW_ID)) is None              # no second account was made
    user = asyncio.run(store.get_user(OLD_ID))
    assert user.plan == "pro" and user.stripe_customer_id == "cus_123"
    _call(mcp, "list_projects")
    assert len(calls) == 1                                          # looked up once, then remembered


def test_signing_in_through_staging_is_unchanged(monkeypatch):
    store = _store_with_existing_account()
    mcp, calls = _server(store, monkeypatch, (OLD_ID, "", STAGING))
    assert "Thesis" in _call(mcp, "list_projects")
    assert calls == [] and asyncio.run(store.get_link(OLD_ID)) is None


def test_a_new_person_signing_in_through_production_gets_a_new_account(monkeypatch):
    store = _store_with_existing_account()
    mcp, _ = _server(store, monkeypatch, ("user_01NEWPERSON", "", PROD), lookup=("new@example.com", True))
    assert "No projects connected yet" in _call(mcp, "list_projects")
    new = asyncio.run(store.get_user("user_01NEWPERSON"))
    assert new is not None and new.email == "new@example.com" and new.plan == "free"


class _ProdWebAuth:
    enabled = True

    def authorization_url(self, *, redirect_uri: str, state: str) -> str:
        return f"{PROD}/authorize?state={state}"

    async def authenticate(self, code: str) -> tuple[str, str]:
        return NEW_ID, "ada@example.com"


def test_website_sign_in_through_production_opens_the_existing_account(monkeypatch):
    store = _store_with_existing_account()
    mcp, _ = _server(store, monkeypatch, (NEW_ID, "", PROD), signin="production", web_auth=_ProdWebAuth())
    with TestClient(mcp.http_app(), base_url="https://testserver") as client:
        client.get("/login", follow_redirects=False)
        state = client.cookies.get("mila_oauth_state")
        r = client.get("/callback", params={"code": "abc", "state": state}, follow_redirects=False)
        assert r.status_code == 303
        page = client.get("/account").text
    assert "Pro" in page
    assert asyncio.run(store.get_user(NEW_ID)) is None
