"""WorkOS AuthKit login for the MCP endpoint, with two deliberate widenings.

1. Audience. FastMCP's AuthKitProvider accepts only tokens whose audience (``aud``) is this
server's address (https://milatexai.com/mcp). WorkOS puts that address in the token only when
the client names it (RFC 8707 ``resource``) or a default is configured in the WorkOS
dashboard. A connection that was authorized without it gets a token addressed to our
WorkOS *environment client id* instead, and WorkOS keeps that audience on every refresh.
Claude does not sign in again after a 401, so such a connection is dead for good: the
user sees "the connector stopped working" until they remove and re-add it.

This provider also accepts that one extra audience. It is safe here because the
WorkOS environment holds only MiLatexAI (nothing else could be the intended recipient),
and everything else is still checked: signature against WorkOS's published keys, issuer,
expiry. A token addressed to any other audience is still refused.

2. A second WorkOS environment (``also_trust``). MiLatexAI is moving from a WorkOS staging
environment to a production one. New sign-ins go to the one advertised here; tokens from
the other are still accepted, with the same checks against THAT environment's keys,
issuer and client id, so connections made before the move keep working.
"""

from __future__ import annotations

from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.auth.providers.workos import AuthKitProvider


def _domain(d: str) -> str:
    d = (d or "").strip().rstrip("/")
    return d if not d or d.startswith("http") else "https://" + d


class MiLatexAIAuthKit(AuthKitProvider):
    def __init__(self, *, extra_audiences: list[str] | tuple[str, ...] = (),
                 also_trust: list[tuple[str, str]] | tuple = (), **kwargs) -> None:
        super().__init__(**kwargs)
        self._extra_audiences = [a.strip() for a in extra_audiences if a and a.strip()]
        # (environment domain, its client id) for each other WorkOS environment trusted.
        self._other = [(_domain(d), (c or "").strip()) for d, c in also_trust if d and d.strip()]
        self._other_verifiers = [
            JWTVerifier(jwks_uri=f"{d}/oauth2/jwks", issuer=d, algorithm="RS256") for d, _ in self._other
        ]

    def set_mcp_path(self, mcp_path: str | None) -> None:
        super().set_mcp_path(mcp_path)
        verifier = self.token_verifier
        if not isinstance(verifier, JWTVerifier):
            return
        current = verifier.audience
        resource = current if isinstance(current, str) else (current[0] if current else None)
        if not resource:
            return
        if self._extra_audiences:   # idempotent: always rebuilt from the resource URL, never grown
            verifier.audience = [resource, *[a for a in self._extra_audiences if a != resource]]
        for (_, client_id), other in zip(self._other, self._other_verifiers):
            other.audience = [resource, *([client_id] if client_id and client_id != resource else [])]

    async def verify_token(self, token: str) -> AccessToken | None:
        found = await self.token_verifier.verify_token(token)
        if found is not None:
            return found
        for other in self._other_verifiers:
            if not other.audience:      # never accept a token without checking who it is for
                continue
            try:
                found = await other.verify_token(token)
            except Exception:  # noqa: BLE001  (a bad token for one environment is just "not this one")
                found = None
            if found is not None:
                return found
        return None
