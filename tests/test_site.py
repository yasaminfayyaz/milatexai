"""Tests for the public marketing site and the ChatGPT-compatibility wiring
(stateless HTTP + CORS for the OpenAI origins)."""

from __future__ import annotations

import warnings

from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.testclient import TestClient

from leafbridge import site
from leafbridge.hosted import create_hosted_server
from leafbridge.store import InMemoryStore, TokenCipher

warnings.filterwarnings("ignore", category=DeprecationWarning)


def _cipher() -> TokenCipher:
    return TokenCipher(TokenCipher.generate_key())


def _server():
    return create_hosted_server(
        store=InMemoryStore(),
        cipher=_cipher(),
        auth=False,
        identity_provider=lambda: ("u", "e"),
        base_url="https://milatexai.com",
    )


# --- marketing site --------------------------------------------------------

def test_site_renders_and_embeds_languages():
    html = site.render_site()
    assert "<!doctype html>" in html.lower()
    assert "data-i18n=" in html
    # Every configured language should be embedded for client-side switching.
    content = site.load_content()
    for lang in content:
        assert f'"{lang}"' in html


def test_site_content_has_all_languages_complete():
    content = site.load_content()
    # Expect the full multilingual set (English + translations), not just fallback.
    assert "en" in content
    assert len(content) >= 6, f"expected many languages, got {list(content)}"
    required = ["hero", "features", "pricing", "privacy", "terms", "faq"]
    for lang, c in content.items():
        for key in required:
            assert key in c, f"{lang} missing {key}"


def test_account_placeholder_renders():
    html = site.render_account_placeholder()
    assert "subscription" in html.lower()


def test_landing_route_serves_marketing_site():
    with TestClient(_server().http_app()) as client:
        r = client.get("/")
    assert r.status_code == 200
    assert "data-i18n=" in r.text


def test_account_route():
    with TestClient(_server().http_app()) as client:
        r = client.get("/account")
    assert r.status_code == 200


# --- ChatGPT compatibility -------------------------------------------------

def test_stateless_http_app_builds_and_serves():
    app = _server().http_app(stateless_http=True)
    with TestClient(app) as client:
        r = client.get("/")
    assert r.status_code == 200


def test_cors_allows_chatgpt_origin():
    cors = Middleware(
        CORSMiddleware,
        allow_origins=["https://chatgpt.com", "https://chat.openai.com"],
        allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
        allow_headers=["Content-Type", "Authorization", "MCP-Protocol-Version"],
    )
    app = _server().http_app(stateless_http=True, middleware=[cors])
    with TestClient(app) as client:
        r = client.options(
            "/mcp",
            headers={
                "Origin": "https://chatgpt.com",
                "Access-Control-Request-Method": "POST",
            },
        )
    assert r.headers.get("access-control-allow-origin") == "https://chatgpt.com"


def test_cors_does_not_echo_unknown_origin():
    cors = Middleware(
        CORSMiddleware,
        allow_origins=["https://chatgpt.com"],
        allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
        allow_headers=["Content-Type", "Authorization"],
    )
    app = _server().http_app(stateless_http=True, middleware=[cors])
    with TestClient(app) as client:
        r = client.options(
            "/mcp",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
    assert r.headers.get("access-control-allow-origin") != "https://evil.example.com"
