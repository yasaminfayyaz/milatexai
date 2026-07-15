"""Signed website login sessions.

When a user signs in on the website through WorkOS AuthKit (the same identity
they use for the Claude/ChatGPT connector), we hand their browser a cookie that
proves who they are on later requests. The cookie is a Fernet token (reusing the
deployment's :class:`TokenCipher` key): authenticated encryption tamper-proofs
it, and Fernet's embedded timestamp gives us expiry.

This is deliberately separate from :mod:`leafbridge.connect_link`. Both sign a
``(user_id, email)`` payload with the same key, so we namespace each with a
``kind`` field and refuse to cross-accept them: a 15-minute connect capability
code must never be usable as a month-long login session, nor the reverse (a login
session must not authorize the token-management forms).
"""

from __future__ import annotations

import json

from .store import TokenCipher, TokenDecryptError

# A website login lasts 30 days, then the user signs in again.
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
_KIND = "sess"


class SessionError(Exception):
    """The session cookie is missing, malformed, tampered, or expired."""


def mint_session(cipher: TokenCipher, user_id: str, email: str) -> str:
    """Mint a login-session cookie value binding a browser to ``user_id``."""
    payload = json.dumps(
        {"k": _KIND, "u": user_id, "e": email}, separators=(",", ":")
    )
    return cipher.encrypt(payload)


def verify_session(
    cipher: TokenCipher, cookie: str, *, ttl: int = SESSION_TTL_SECONDS
) -> tuple[str, str]:
    """Return ``(user_id, email)`` for a valid, unexpired session cookie, else
    raise :class:`SessionError`."""
    if not cookie:
        raise SessionError("Missing session cookie.")
    try:
        payload = cipher.decrypt(cookie, ttl=ttl)
    except TokenDecryptError as exc:
        raise SessionError("Session is invalid or expired.") from exc
    try:
        data = json.loads(payload)
    except ValueError as exc:
        raise SessionError("Malformed session cookie.") from exc
    if not isinstance(data, dict) or data.get("k") != _KIND:
        # A connect code (or anything else) signed with the same key is not a
        # login session — reject rather than silently accept.
        raise SessionError("Not a login session.")
    user_id = data.get("u")
    if not user_id:
        raise SessionError("Session is missing a user.")
    return user_id, data.get("e", "")
