"""Tests for the web onboarding flow: the connect-code capability crypto and the
/connect web routes that keep the Overleaf token out of the chat transcript.
"""

from __future__ import annotations

import asyncio
import warnings

import pytest
from starlette.testclient import TestClient

from leafbridge import connect_link
from leafbridge.connect_link import (
    ConnectCodeError,
    mint_connect_code,
    verify_connect_code,
)
from leafbridge.hosted import create_hosted_server
from leafbridge.store import InMemoryStore, TokenCipher

HEX = "0123456789abcdef01234567"
OVERLEAF_URL = f"https://www.overleaf.com/project/{HEX}"

# The Starlette TestClient warns that its httpx shim is deprecated; irrelevant here.
warnings.filterwarnings("ignore", category=DeprecationWarning)


# --- connect-code crypto ---------------------------------------------------

def _cipher() -> TokenCipher:
    return TokenCipher(TokenCipher.generate_key())


def test_connect_code_round_trips_identity():
    cip = _cipher()
    code = mint_connect_code(cip, "user_42", "a@b.com")
    assert verify_connect_code(cip, code) == ("user_42", "a@b.com")


def test_connect_code_rejects_expired():
    cip = _cipher()
    code = mint_connect_code(cip, "user_42", "a@b.com")
    # A zero-second TTL means anything but a just-minted token is stale.
    with pytest.raises(ConnectCodeError):
        verify_connect_code(cip, code, ttl=-1)


def test_connect_code_rejects_tampering_and_wrong_key():
    cip = _cipher()
    code = mint_connect_code(cip, "user_42", "a@b.com")
    with pytest.raises(ConnectCodeError):
        verify_connect_code(cip, code[:-4] + "AAAA")
    with pytest.raises(ConnectCodeError):
        verify_connect_code(_cipher(), code)  # different key


def test_connect_code_rejects_empty():
    with pytest.raises(ConnectCodeError):
        verify_connect_code(_cipher(), "")


# --- web routes ------------------------------------------------------------

@pytest.fixture
def harness():
    store = InMemoryStore()
    cipher = _cipher()
    mcp = create_hosted_server(
        store=store,
        cipher=cipher,
        auth=False,
        identity_provider=lambda: ("user_web", "web@example.com"),
        base_url="https://milatexai.com",
    )
    return store, cipher, mcp


def _projects(store, user_id):
    return asyncio.run(store.list_projects(user_id))


def test_landing_page_renders(harness):
    _store, _cipher, mcp = harness
    with TestClient(mcp.http_app()) as client:
        r = client.get("/")
    assert r.status_code == 200
    assert "MiLatexAI" in r.text or "LaTeX" in r.text


def test_connect_get_shows_form_for_valid_code(harness):
    _store, cipher, mcp = harness
    code = mint_connect_code(cipher, "user_web", "web@example.com")
    with TestClient(mcp.http_app()) as client:
        r = client.get("/connect", params={"code": code})
    assert r.status_code == 200
    assert "Connect an Overleaf project" in r.text
    assert "web@example.com" in r.text  # signed-in-as line
    assert "name='token'" in r.text
    assert "type='password'" in r.text  # token field is masked


def test_connect_get_rejects_bad_code(harness):
    _store, _cipher, mcp = harness
    with TestClient(mcp.http_app()) as client:
        r = client.get("/connect", params={"code": "not-a-real-code"})
    assert r.status_code == 400
    assert "expired" in r.text.lower() or "invalid" in r.text.lower()


def test_connect_post_stores_encrypted_token_and_succeeds(harness):
    store, cipher, mcp = harness
    code = mint_connect_code(cipher, "user_web", "web@example.com")
    with TestClient(mcp.http_app()) as client:
        r = client.post(
            "/connect",
            data={
                "code": code,
                "overleaf_url": OVERLEAF_URL,
                "token": "olp_realtoken123",
                "name": "thesis",
            },
        )
    assert r.status_code == 200
    assert "Connected" in r.text

    projects = _projects(store, "user_web")
    assert len(projects) == 1
    p = projects[0]
    assert p.project_id == HEX
    assert p.name == "thesis"
    # The token is stored at the ACCOUNT level now (one token, many projects);
    # the project itself carries no token.
    assert p.token_encrypted == ""
    user = asyncio.run(store.get_user("user_web"))
    assert "olp_realtoken123" not in user.overleaf_token_encrypted
    assert cipher.decrypt(user.overleaf_token_encrypted) == "olp_realtoken123"


def test_connect_link_reusable_within_ttl(harness):
    store, cipher, mcp = harness
    code = mint_connect_code(cipher, "user_web", "web@example.com")
    payload = {
        "code": code,
        "overleaf_url": OVERLEAF_URL,
        "token": "olp_realtoken123",
        "name": "thesis",
    }
    with TestClient(mcp.http_app()) as client:
        first = client.post("/connect", data=payload)
        second = client.post("/connect", data=payload)
    # Codes are reusable within their TTL (the manage forms submit repeatedly);
    # re-submitting the same project just updates it — no duplicate.
    assert first.status_code == 200
    assert second.status_code == 200
    assert len(_projects(store, "user_web")) == 1


def test_connect_post_missing_token_reprompts(harness):
    store, cipher, mcp = harness
    code = mint_connect_code(cipher, "user_web", "web@example.com")
    with TestClient(mcp.http_app()) as client:
        r = client.post(
            "/connect",
            data={"code": code, "overleaf_url": OVERLEAF_URL, "token": ""},
        )
    assert r.status_code == 400
    assert "Git token" in r.text
    assert _projects(store, "user_web") == []


def test_resolve_or_onboard_returns_link_when_no_project(tmp_path):
    from fastmcp.exceptions import ToolError

    from leafbridge.hosted import HostedApp
    from leafbridge.store import InMemoryStore, User

    cipher = _cipher()
    app = HostedApp(store=InMemoryStore(), cipher=cipher, data_dir=tmp_path,
                    base_url="https://milatexai.com")
    user = asyncio.run(app.service.get_or_create_user("u1", "u@x.com"))
    # No project yet -> any file action should hand back a secure connect link,
    # not a dead error (so the user never needs to know start_connect).
    try:
        asyncio.run(app.resolve_or_onboard(user, None))
        raise AssertionError("expected onboarding ToolError")
    except ToolError as exc:
        assert "milatexai.com/connect?code=" in str(exc)
    # Once a project is connected, it resolves normally (by default / by name).
    asyncio.run(app.service.connect_project("u1", OVERLEAF_URL, "olp_tok", "thesis"))
    proj = asyncio.run(app.resolve_or_onboard(user, "thesis"))
    assert proj.project_id == HEX


def test_connect_post_does_not_echo_token_on_error(harness):
    _store, cipher, mcp = harness
    code = mint_connect_code(cipher, "user_web", "web@example.com")
    with TestClient(mcp.http_app()) as client:
        # Bad URL triggers a validation error; token must not be reflected back.
        r = client.post(
            "/connect",
            data={
                "code": code,
                "overleaf_url": "https://example.com/not-overleaf",
                "token": "olp_secretshouldnotecho",
            },
        )
    assert "olp_secretshouldnotecho" not in r.text
