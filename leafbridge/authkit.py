"""WorkOS AuthKit login for the MCP endpoint, with one deliberate widening.

FastMCP's AuthKitProvider accepts only tokens whose audience (``aud``) is this server's
address (https://milatexai.com/mcp). WorkOS puts that address in the token only when
the client names it (RFC 8707 ``resource``) or a default is configured in the WorkOS
dashboard. A connection that was authorized without it gets a token addressed to our
WorkOS *environment client id* instead, and WorkOS keeps that audience on every refresh.
Claude does not sign in again after a 401, so such a connection is dead for good: the
user sees "the connector stopped working" until they remove and re-add it.

This provider also accepts that one extra audience. It is safe here because the
WorkOS environment holds only MiLatexAI (nothing else could be the intended recipient),
and everything else is still checked: signature against WorkOS's published keys, issuer,
expiry. A token addressed to any other audience is still refused.
"""

from __future__ import annotations

from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.auth.providers.workos import AuthKitProvider


class MiLatexAIAuthKit(AuthKitProvider):
    def __init__(self, *, extra_audiences: list[str] | tuple[str, ...] = (), **kwargs) -> None:
        super().__init__(**kwargs)
        self._extra_audiences = [a.strip() for a in extra_audiences if a and a.strip()]

    def set_mcp_path(self, mcp_path: str | None) -> None:
        super().set_mcp_path(mcp_path)
        verifier = self.token_verifier
        if not self._extra_audiences or not isinstance(verifier, JWTVerifier):
            return
        current = verifier.audience
        resource = current if isinstance(current, str) else (current[0] if current else None)
        if resource:   # idempotent: always rebuilt from the resource URL, never grown
            verifier.audience = [resource, *[a for a in self._extra_audiences if a != resource]]
