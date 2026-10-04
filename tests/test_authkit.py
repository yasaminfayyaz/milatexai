"""The audience rule of the login provider (leafbridge/authkit.py), without a network."""

from __future__ import annotations

from leafbridge.authkit import MiLatexAIAuthKit

DOMAIN = "https://example.authkit.app"
BASE = "https://milatexai.com"
RESOURCE = "https://milatexai.com/mcp"


def provider(*extra):
    p = MiLatexAIAuthKit(authkit_domain=DOMAIN, base_url=BASE, extra_audiences=list(extra))
    p.set_mcp_path("/mcp")
    return p


def test_default_is_only_this_server():
    assert provider().token_verifier.audience == RESOURCE


def test_client_id_is_accepted_in_addition_to_the_server_address():
    assert provider("client_abc").token_verifier.audience == [RESOURCE, "client_abc"]


def test_blank_and_duplicate_extras_are_ignored():
    assert provider("", "  ", None).token_verifier.audience == RESOURCE
    assert provider(RESOURCE).token_verifier.audience == [RESOURCE]


def test_setting_the_path_twice_does_not_grow_the_list():
    p = provider("client_abc")
    p.set_mcp_path("/mcp")
    assert p.token_verifier.audience == [RESOURCE, "client_abc"]
