"""Tests for website sign-in (WorkOS AuthKit) and web-based billing management:
the signed session cookie, the /login -> /callback OAuth flow, and the
/account* routes that let a signed-in user upgrade or cancel on the web.
"""

from __future__ import annotations

import asyncio
import warnings

import pytest
from starlette.testclient import TestClient

from leafbridge.connect_link import ConnectCodeError, mint_connect_code, verify_connect_code
from leafbridge.hosted import create_hosted_server
from leafbridge.store import InMemoryStore, Project, TokenCipher, User
from leafbridge.web_session import SessionError, mint_session, verify_session
from leafbridge.workos_web import WorkOSWebAuth

warnings.filterwarnings("ignore", category=DeprecationWarning)


def _cipher() -> TokenCipher:
    return TokenCipher(TokenCipher.generate_key())


# --- session cookie crypto -------------------------------------------------

def test_session_round_trips_identity():
    cip = _cipher()
    cookie = mint_session(cip, "user_42", "a@b.com")
    assert verify_session(cip, cookie) == ("user_42", "a@b.com")


def test_session_rejects_expired():
    cip = _cipher()
    cookie = mint_session(cip, "user_42", "a@b.com")
    with pytest.raises(SessionError):
        verify_session(cip, cookie, ttl=-1)


def test_session_rejects_tamper_and_wrong_key_and_empty():
    cip = _cipher()
    cookie = mint_session(cip, "user_42", "a@b.com")
    with pytest.raises(SessionError):
        verify_session(cip, cookie[:-4] + "AAAA")
    with pytest.raises(SessionError):
        verify_session(_cipher(), cookie)
    with pytest.raises(SessionError):
        verify_session(cip, "")


def test_connect_code_and_session_are_not_interchangeable():
    """A 15-min connect capability code must never work as a 30-day login
    session, nor a login session as a connect code (both use the same key)."""
    cip = _cipher()
    session = mint_session(cip, "user_42", "a@b.com")
    connect = mint_connect_code(cip, "user_42", "a@b.com")
    # session cookie is not a connect code:
    with pytest.raises(ConnectCodeError):
        verify_connect_code(cip, session)
    # connect code is not a session cookie:
    with pytest.raises(SessionError):
        verify_session(cip, connect)


# --- authorization URL -----------------------------------------------------

def test_authorization_url_shape():
    wa = WorkOSWebAuth(api_key="sk_test_x", client_id="client_abc")
    url = wa.authorization_url(redirect_uri="https://milatexai.com/callback", state="xyz")
    assert url.startswith("https://api.workos.com/user_management/authorize?")
    assert "response_type=code" in url
    assert "client_id=client_abc" in url
    assert "provider=authkit" in url
    assert "redirect_uri=https%3A%2F%2Fmilatexai.com%2Fcallback" in url
    assert "state=xyz" in url


def test_web_auth_enabled_flag():
    assert WorkOSWebAuth(api_key="k", client_id="c").enabled is True
    assert WorkOSWebAuth(api_key="", client_id="c").enabled is False
    assert WorkOSWebAuth(api_key="k", client_id="").enabled is False


# --- route harness ---------------------------------------------------------

class FakeWebAuth:
    enabled = True

    def authorization_url(self, *, redirect_uri: str, state: str) -> str:
        return (
            "https://api.workos.com/user_management/authorize"
            f"?client_id=client_test&redirect_uri={redirect_uri}"
            f"&provider=authkit&state={state}&response_type=code"
        )

    async def authenticate(self, code: str) -> tuple[str, str]:
        return "user_web", "web@example.com"


class FakeBilling:
    enabled = True

    def __init__(self):
        self.checkout_calls = []
        self.portal_calls = []

    async def create_checkout(self, user, customer_id=None):
        self.checkout_calls.append((user.user_id, customer_id))
        return "https://checkout.example/session", "cus_fake"

    async def create_portal(self, customer_id):
        self.portal_calls.append(customer_id)
        return "https://portal.example/session"


def _harness(*, web_auth=None, billing=None):
    store = InMemoryStore()
    cipher = _cipher()
    mcp = create_hosted_server(
        store=store,
        cipher=cipher,
        auth=False,
        identity_provider=lambda: ("user_web", "web@example.com"),
        base_url="https://milatexai.com",
        web_auth=web_auth if web_auth is not None else FakeWebAuth(),
        billing=billing,
    )
    return store, cipher, mcp


def _client(mcp):
    # https base_url so the Secure session cookie round-trips in the jar.
    return TestClient(mcp.http_app(), base_url="https://testserver")


# --- /account (logged out vs in) -------------------------------------------

def test_account_logged_out_shows_sign_in():
    _store, _cipher, mcp = _harness()
    with _client(mcp) as client:
        r = client.get("/account")
    assert r.status_code == 200
    assert "Sign in" in r.text
    assert 'href="/login"' in r.text or "href='/login'" in r.text


def test_account_signed_in_free_shows_upgrade():
    store, cipher, mcp = _harness(billing=FakeBilling())
    with _client(mcp) as client:
        client.cookies.set("mila_session", mint_session(cipher, "user_web", "web@example.com"))
        r = client.get("/account")
    assert r.status_code == 200
    assert "Free plan" in r.text
    assert "/account/upgrade" in r.text
    assert "Signed in as web@example.com" in r.text


def test_account_signed_in_pro_shows_manage():
    store, cipher, mcp = _harness(billing=FakeBilling())
    # Seed a pro user with a Stripe customer id.
    import asyncio
    asyncio.run(
        store.upsert_user(User(user_id="user_web", email="web@example.com",
                               plan="pro", stripe_customer_id="cus_x"))
    )
    with _client(mcp) as client:
        client.cookies.set("mila_session", mint_session(cipher, "user_web", "web@example.com"))
        r = client.get("/account")
    assert "on Pro" in r.text  # heading "You're on Pro" (apostrophe HTML-escaped)
    assert "/account/manage" in r.text


def test_account_shows_setup_nudge_when_no_project():
    store, cipher, mcp = _harness(billing=FakeBilling())
    with _client(mcp) as client:
        client.cookies.set("mila_session", mint_session(cipher, "user_web", "web@example.com"))
        r = client.get("/account")
    assert "Next step: start editing" in r.text
    assert "/#get-started" in r.text


def test_account_hides_setup_nudge_when_project_connected():
    store, cipher, mcp = _harness(billing=FakeBilling())
    asyncio.run(store.put_project(Project(user_id="user_web", project_id="deadbeef", name="thesis")))
    with _client(mcp) as client:
        client.cookies.set("mila_session", mint_session(cipher, "user_web", "web@example.com"))
        r = client.get("/account")
    assert "Next step: start editing" not in r.text


# --- /login + /callback flow ----------------------------------------------

def test_login_redirects_to_authkit_and_sets_state_cookie():
    _store, _cipher, mcp = _harness(web_auth=WorkOSWebAuth(api_key="k", client_id="client_abc"))
    with _client(mcp) as client:
        r = client.get("/login", follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "api.workos.com/user_management/authorize" in loc
    assert "client_id=client_abc" in loc
    assert "provider=authkit" in loc
    assert "redirect_uri=https%3A%2F%2Fmilatexai.com%2Fcallback" in loc
    set_cookie = " ".join(r.headers.get_list("set-cookie")).lower()
    assert "mila_oauth_state=" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "secure" in set_cookie


def test_login_disabled_when_web_auth_off():
    _store, _cipher, mcp = _harness(web_auth=WorkOSWebAuth(api_key="", client_id=""))
    with _client(mcp) as client:
        r = client.get("/login", follow_redirects=False)
    assert r.status_code == 503


def test_callback_completes_sign_in_and_sets_session():
    store, _cipher, mcp = _harness()
    with _client(mcp) as client:
        # /login sets the state cookie in the jar.
        login = client.get("/login", follow_redirects=False)
        state = client.cookies.get("mila_oauth_state")
        assert state
        r = client.get("/callback", params={"code": "abc", "state": state},
                       follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/account"
        assert client.cookies.get("mila_session")
        # Now logged in.
        acct = client.get("/account")
    assert "Free plan" in acct.text
    # The user was created in the store during callback.
    import asyncio
    user = asyncio.run(store.get_user("user_web"))
    assert user is not None and user.email == "web@example.com"


def test_callback_rejects_state_mismatch():
    _store, _cipher, mcp = _harness()
    with _client(mcp) as client:
        client.get("/login", follow_redirects=False)  # sets a state cookie
        r = client.get("/callback", params={"code": "abc", "state": "not-the-state"},
                       follow_redirects=False)
    assert r.status_code == 400
    assert "sign-in" in r.text.lower() or "invalid" in r.text.lower()


def test_callback_rejects_missing_code():
    _store, _cipher, mcp = _harness()
    with _client(mcp) as client:
        login = client.get("/login", follow_redirects=False)
        state = client.cookies.get("mila_oauth_state")
        r = client.get("/callback", params={"state": state}, follow_redirects=False)
    assert r.status_code == 400


# --- billing actions (auth + CSRF posture) ---------------------------------

def test_upgrade_requires_session_redirects_to_login():
    _store, _cipher, mcp = _harness(billing=FakeBilling())
    with _client(mcp) as client:
        r = client.post("/account/upgrade", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_manage_requires_session_redirects_to_login():
    _store, _cipher, mcp = _harness(billing=FakeBilling())
    with _client(mcp) as client:
        r = client.post("/account/manage", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_upgrade_signed_in_free_starts_checkout():
    store, cipher, mcp = _harness(billing=FakeBilling())
    with _client(mcp) as client:
        client.cookies.set("mila_session", mint_session(cipher, "user_web", "web@example.com"))
        r = client.post("/account/upgrade", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "https://checkout.example/session"
    # Stripe customer id was remembered.
    import asyncio
    user = asyncio.run(store.get_user("user_web"))
    assert user.stripe_customer_id == "cus_fake"


def test_manage_signed_in_pro_opens_portal():
    store, cipher, mcp = _harness(billing=FakeBilling())
    import asyncio
    asyncio.run(
        store.upsert_user(User(user_id="user_web", email="web@example.com",
                               plan="pro", stripe_customer_id="cus_x"))
    )
    with _client(mcp) as client:
        client.cookies.set("mila_session", mint_session(cipher, "user_web", "web@example.com"))
        r = client.post("/account/manage", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "https://portal.example/session"


def test_admin_upgrade_is_noop_redirect():
    store, cipher, mcp = _harness(billing=FakeBilling())
    import asyncio
    asyncio.run(
        store.upsert_user(User(user_id="user_web", email="web@example.com", is_admin=True))
    )
    with _client(mcp) as client:
        client.cookies.set("mila_session", mint_session(cipher, "user_web", "web@example.com"))
        r = client.post("/account/upgrade", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/account"


def test_logout_clears_session_cookie():
    _store, cipher, mcp = _harness()
    with _client(mcp) as client:
        client.cookies.set("mila_session", mint_session(cipher, "user_web", "web@example.com"))
        r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    set_cookie = " ".join(r.headers.get_list("set-cookie")).lower()
    assert "mila_session=" in set_cookie  # cleared (expired) cookie
