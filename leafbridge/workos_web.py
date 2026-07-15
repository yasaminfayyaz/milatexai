"""Website login via WorkOS AuthKit (User Management OAuth).

Lets a user sign in on the marketing site with the SAME WorkOS identity they use
for the Claude/ChatGPT connector, so they can manage billing on the web. We speak
the User Management OAuth endpoints directly over HTTP (same style as the email
resolver in :mod:`leafbridge.hosted`) to avoid pulling in the WorkOS SDK.

Flow:
  1. ``/login``    -> redirect the browser to :meth:`authorization_url` (AuthKit
                      hosted sign-in).
  2. the user authenticates with WorkOS.
  3. ``/callback?code=...`` -> :meth:`authenticate` -> ``(user_id, email)``.

The returned user id is the same ``user_...`` value that appears as the ``sub``
claim on the connector's access tokens, so it keys the very same store record.
"""

from __future__ import annotations

import json
from urllib.parse import urlencode

_AUTHORIZE_URL = "https://api.workos.com/user_management/authorize"
_AUTHENTICATE_URL = "https://api.workos.com/user_management/authenticate"


class WorkOSAuthError(Exception):
    """The WorkOS OAuth exchange failed."""


class WorkOSWebAuth:
    """Minimal WorkOS User Management OAuth client for the web sign-in flow."""

    def __init__(self, *, api_key: str, client_id: str):
        self._api_key = api_key
        self._client_id = client_id
        self.enabled = bool(api_key and client_id)

    def authorization_url(self, *, redirect_uri: str, state: str) -> str:
        """The AuthKit hosted sign-in URL to redirect the browser to.

        ``provider=authkit`` selects the deployment's AuthKit UI; ``state`` is
        echoed back to ``/callback`` and we use it for CSRF protection.
        """
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._client_id,
                "redirect_uri": redirect_uri,
                "provider": "authkit",
                "state": state,
            }
        )
        return f"{_AUTHORIZE_URL}?{query}"

    async def authenticate(self, code: str) -> tuple[str, str]:
        """Exchange an authorization code for ``(user_id, email)``.

        The WorkOS API key is the client secret for the confidential-client
        ``authorization_code`` grant. Raises :class:`WorkOSAuthError` on failure.
        """
        if not self.enabled:
            raise WorkOSAuthError("Web sign-in is not configured.")
        import aiohttp

        body = {
            "client_id": self._client_id,
            "client_secret": self._api_key,
            "grant_type": "authorization_code",
            "code": code,
        }
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(_AUTHENTICATE_URL, json=body) as resp:
                    raw = await resp.text()
                    status = resp.status
        except aiohttp.ClientError as exc:  # network / DNS / timeout
            raise WorkOSAuthError(f"WorkOS request failed: {exc}") from exc

        try:
            data = json.loads(raw)
        except ValueError:
            data = {}
        if status != 200:
            msg = data.get("error_description") or data.get("error") or f"HTTP {status}"
            raise WorkOSAuthError(f"WorkOS authentication failed: {msg}")
        user = data.get("user") or {}
        user_id = user.get("id")
        if not user_id:
            raise WorkOSAuthError("WorkOS response contained no user id.")
        return user_id, user.get("email") or ""
