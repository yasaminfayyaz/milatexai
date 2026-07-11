"""One-time connect links, so a user's Overleaf Git token is pasted into a web
form instead of the chat transcript.

The flow: the user runs the ``start_connect`` tool inside Claude (already
authenticated via WorkOS). We mint a short-lived, tamper-proof *capability code*
that encodes their identity, and hand back a ``…/connect?code=…`` URL. Opening it
shows a form; submitting it stores the token encrypted. The token never appears
in the conversation.

The code is a Fernet token (reusing the deployment's :class:`TokenCipher` key):
authenticated encryption gives us tamper-proofing for free, and Fernet's embedded
timestamp gives us expiry. The code is NOT a secret in the credential sense — it
only authorizes *setting the user's own* Overleaf token — but it is still bound to
one user and expires quickly, and we enforce best-effort single use on top.
"""

from __future__ import annotations

import json

from .store import TokenCipher, TokenDecryptError

# A connect link is valid for 15 minutes and (best-effort) usable once.
CONNECT_TTL_SECONDS = 15 * 60


class ConnectCodeError(Exception):
    """The connect code is missing, malformed, tampered, or expired."""


def mint_connect_code(cipher: TokenCipher, user_id: str, email: str) -> str:
    """Mint a capability code binding this browser session to ``user_id``."""
    payload = json.dumps({"u": user_id, "e": email}, separators=(",", ":"))
    return cipher.encrypt(payload)


def verify_connect_code(
    cipher: TokenCipher, code: str, *, ttl: int = CONNECT_TTL_SECONDS
) -> tuple[str, str]:
    """Return ``(user_id, email)`` for a valid, unexpired code, else raise
    :class:`ConnectCodeError`."""
    if not code:
        raise ConnectCodeError("Missing connect code.")
    try:
        payload = cipher.decrypt(code, ttl=ttl)
    except TokenDecryptError as exc:
        raise ConnectCodeError(
            "This connect link is invalid or has expired. Run start_connect in "
            "Claude to get a fresh one."
        ) from exc
    try:
        data = json.loads(payload)
        user_id = data["u"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ConnectCodeError("Malformed connect code.") from exc
    if not user_id:
        raise ConnectCodeError("Connect code is missing a user.")
    return user_id, data.get("e", "")
