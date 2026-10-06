"""Tests for the public marketing site and the ChatGPT-compatibility wiring
(stateless HTTP + CORS for the OpenAI origins)."""

from __future__ import annotations

import warnings

from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.testclient import TestClient

from leafbridge import site
from leafbridge.capacity import CapacityGate
from leafbridge.hosted import create_hosted_server
from leafbridge.store import InMemoryStore, TokenCipher

warnings.filterwarnings("ignore", category=DeprecationWarning)


def _cipher() -> TokenCipher:
    return TokenCipher(TokenCipher.generate_key())


def _server():
    # Inject an explicitly-disabled capacity gate so the test is deterministic
    # regardless of any AZURE_SUBSCRIPTION_ID in the ambient environment.
    return create_hosted_server(
        store=InMemoryStore(),
        cipher=_cipher(),
        auth=False,
        identity_provider=lambda: ("u", "e"),
        base_url="https://milatexai.com",
        capacity=CapacityGate(subscription_id="", resource_group="", stripe_api_key=""),
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


def test_account_page_renders_and_reacts_to_status():
    assert "subscription" in site.render_account().lower()
    assert "pro" in site.render_account(status="success").lower()
    assert "cancel" in site.render_account(status="cancelled").lower()


def test_landing_route_serves_marketing_site():
    with TestClient(_server().http_app()) as client:
        r = client.get("/")
    assert r.status_code == 200
    assert "data-i18n=" in r.text


def test_account_route():
    with TestClient(_server().http_app()) as client:
        r = client.get("/account")
    assert r.status_code == 200


def test_tools_hub_page_and_route():
    page = site.render_tools_page()
    assert page.lstrip().lower().startswith("<!doctype html>")
    # links to both tools, and back to the product
    assert "/tools/bibtex" in page and "/tools/latex-error-finder" in page
    assert "milatexai.com/tools" in page  # canonical
    with TestClient(_server().http_app()) as client:
        r = client.get("/tools")
    assert r.status_code == 200
    assert "Free web tools" in r.text
    # edge-cached so it never wakes the container
    from leafbridge import asgi
    assert "/tools" in asgi._SecurityHeaders._EDGE_CACHED


def test_legal_pages_are_separate_and_off_homepage():
    from leafbridge import asgi
    # legal content lives on its own pages, not crowding the homepage
    home = site.render_site()
    assert "id='privacy'" not in home and "id='terms'" not in home
    for kind in ("privacy", "terms"):
        page = site.render_legal_page(kind)
        assert page.lstrip().lower().startswith("<!doctype html>")
        assert f"milatexai.com/{kind}" in page  # canonical
        assert f"/{kind}" in asgi._SecurityHeaders._EDGE_CACHED
    with TestClient(_server().http_app()) as client:
        assert client.get("/privacy").status_code == 200
        assert client.get("/terms").status_code == 200


def test_health_capacity_route():
    with TestClient(_server().http_app()) as client:
        r = client.get("/health/capacity")
    assert r.status_code == 200
    body = r.json()
    # Default server has a disabled gate (no Azure sub) -> free stays open.
    assert body["gating_enabled"] is False
    assert body["free_open"] is True
    assert set(body) == {"gating_enabled", "free_open", "signals_fresh", "latex_available", "figure_studio"}
    assert isinstance(body["latex_available"], bool)


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


def test_feature_badge_renders_on_the_new_card_only():
    """Only the "new capability" card should carry a badge; the old spotlight
    section's own badge must have moved off "New" once a newer feature exists."""
    html = site.render_site()
    assert html.count(">New<") == 1
    assert 'data-i18n="features.10.badge"' in html
    assert "Move files between projects" in html
    # The spotlight ("see") section is no longer the one marked New.
    see_block = html[html.index("id='see'"):html.index("id='how'")]
    assert "New" not in see_block


# --- what the Claude directory reviewers read: help, privacy, security ---------------------------------

def test_help_page_has_setup_examples_and_troubleshooting():
    from leafbridge import site
    page = site.render_legal_page("help")
    assert "https://milatexai.com/mcp" in page and "Things to ask" in page and "Troubleshooting" in page
    assert page.count("&quot;") >= 6 or page.count('"') >= 6           # at least three example prompts
    assert "support@milatexai.com" in page and chr(0x2014) not in page
    assert "https://milatexai.com/help" in site.sitemap_xml()


def test_privacy_policy_names_everything_we_keep_and_every_provider():
    from leafbridge import site
    page = site.render_legal_page("privacy")
    for must in ("email", "account ID", "encrypted", "repository link", "write-commits", "customer ID",
                 "30 days", "WorkOS", "Azure", "Cloudflare", "Stripe", "Overleaf"):
        assert must in page, must
    assert "only three things" not in page and "complete list" not in page


def test_faq_no_longer_claims_we_keep_only_three_things():
    import json
    from pathlib import Path
    d = json.loads((Path(__file__).resolve().parents[1] / "leafbridge" / "site_content.json").read_text(encoding="utf-8"))
    assert "the list of projects you connect" in d["en"]["faq"]["items"][3]["a"]
    assert "only things we keep" not in d["en"]["faq"]["items"][3]["a"]


def test_security_policy_exists_with_a_reporting_channel():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "SECURITY.md").read_text(encoding="utf-8")
    assert "support@milatexai.com" in text and "Security" in text
