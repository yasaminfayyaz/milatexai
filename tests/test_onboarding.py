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
    assert "Connect an Overleaf" in r.text  # Overleaf-first heading
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


def test_connect_get_makes_token_optional_when_account_has_token(harness):
    _store, cipher, mcp = harness
    code = mint_connect_code(cipher, "user_web", "web@example.com")
    with TestClient(mcp.http_app()) as client:
        # First connection stores the account-level token.
        client.post("/connect", data={
            "code": code, "overleaf_url": OVERLEAF_URL,
            "token": "olp_realtoken123", "name": "first"})
        # Re-opening /connect still shows the token field (a GitHub/GitLab repo
        # needs its own token) but tells the user it's OPTIONAL for Overleaf,
        # leaving it blank reuses the saved Overleaf token.
        r = client.get("/connect", params={"code": code})
    assert r.status_code == 200
    assert "reuse your saved Overleaf token" in r.text
    assert "name='token'" in r.text  # always shown now (per-repo tokens)
    assert "(optional for Overleaf)" in r.text


def test_connect_post_reuses_saved_token_for_second_project():
    # Admin so the free 1-project limit doesn't mask the token-reuse behavior.
    store = InMemoryStore()
    cipher = _cipher()
    mcp = create_hosted_server(
        store=store, cipher=cipher, auth=False,
        identity_provider=lambda: ("user_web", "admin@example.com"),
        admin_emails=("admin@example.com",),
        base_url="https://milatexai.com",
    )
    code = mint_connect_code(cipher, "user_web", "admin@example.com")
    second = "https://www.overleaf.com/project/0123456789abcdef01234568"
    with TestClient(mcp.http_app()) as client:
        client.post("/connect", data={
            "code": code, "overleaf_url": OVERLEAF_URL,
            "token": "olp_realtoken123", "name": "first"})
        # Second project, NO token submitted -> must reuse the saved account token.
        r = client.post("/connect", data={
            "code": code, "overleaf_url": second, "name": "second"})
    assert r.status_code == 200
    assert "Connected" in r.text
    assert len(_projects(store, "user_web")) == 2


def test_connect_duplicate_url_shows_already_connected(harness):
    store, cipher, mcp = harness
    code = mint_connect_code(cipher, "user_web", "web@example.com")
    with TestClient(mcp.http_app()) as client:
        client.post("/connect", data={
            "code": code, "overleaf_url": OVERLEAF_URL,
            "token": "olp_tok", "name": "thesis"})
        # Submit the SAME project again -> "already connected", not a duplicate/rename.
        r = client.post("/connect", data={
            "code": code, "overleaf_url": OVERLEAF_URL, "name": "renamed"})
    assert "already" in r.text.lower()
    projects = _projects(store, "user_web")
    assert len(projects) == 1
    assert projects[0].name == "thesis"


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
    # re-submitting the same project just updates it, no duplicate.
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


# --- "How did you find MiLatexAI?" (an anonymous count, asked once) ---------------------------------

def _source_counts(store):
    month = __import__("time").strftime("%Y-%m", __import__("time").gmtime())
    from leafbridge.web import SIGNUP_SOURCES
    return {k: asyncio.run(store.get_usage(f"signup-source:{k}", month)) for k, _ in SIGNUP_SOURCES}


def test_first_connect_form_asks_where_you_found_us_and_the_second_does_not(harness):
    _store, cipher, mcp = harness
    code = mint_connect_code(cipher, "user_web", "web@example.com")
    with TestClient(mcp.http_app()) as client:
        first = client.get("/connect", params={"code": code})
        client.post("/connect", data={"code": code, "overleaf_url": OVERLEAF_URL, "token": "olp_tok", "name": "a"})
        again = client.get("/connect", params={"code": code})
    assert "name='source'" in first.text and "Prefer not to say" in first.text and "(optional)" in first.text
    assert "name='source'" not in again.text


def test_the_answer_is_counted_once_and_not_tied_to_the_person(harness):
    store, cipher, mcp = harness
    code = mint_connect_code(cipher, "user_web", "web@example.com")
    second = "https://www.overleaf.com/project/0123456789abcdef01234568"
    with TestClient(mcp.http_app()) as client:
        r = client.post("/connect", data={"code": code, "overleaf_url": OVERLEAF_URL, "token": "olp_tok",
                                          "name": "a", "source": "reddit"})
        assert r.status_code == 200
        # a later project from the same account must not count again, even if the field is sent
        client.post("/connect", data={"code": code, "overleaf_url": second, "name": "b", "source": "reddit"})
    counts = _source_counts(store)
    assert counts["reddit"] == 1 and sum(counts.values()) == 1
    user = asyncio.run(store.get_user("user_web"))
    assert not hasattr(user, "source")          # nothing about the answer is stored on the user


def test_blank_unknown_and_hostile_answers_are_ignored_and_never_block_connecting(harness):
    store, cipher, mcp = harness
    for i, answer in enumerate(("", "not-a-choice", "<script>alert(1)</script>", "reddit; drop table")):
        code = mint_connect_code(cipher, f"user_{i}", f"u{i}@example.com")
        url = f"https://www.overleaf.com/project/0123456789abcdef0123456{i}"
        with TestClient(mcp.http_app()) as client:
            r = client.post("/connect", data={"code": code, "overleaf_url": url, "token": "olp_tok", "name": "x", "source": answer})
        assert r.status_code == 200 and "Connected" in r.text, answer
    assert sum(_source_counts(store).values()) == 0


def test_a_failed_submission_keeps_the_choice_and_counts_nothing(harness):
    store, cipher, mcp = harness
    code = mint_connect_code(cipher, "user_web", "web@example.com")
    with TestClient(mcp.http_app()) as client:
        r = client.post("/connect", data={"code": code, "overleaf_url": "", "token": "olp_tok", "source": "search"})
    assert r.status_code == 400 and "<option value='search' selected>" in r.text
    assert sum(_source_counts(store).values()) == 0
