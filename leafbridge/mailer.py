"""Customer emails, sent through the Cloudflare Worker that already sends our alert emails.

The app has no mail server of its own. The Worker (ops/watchdog/worker.js) holds the sending
permission and only accepts two fixed payment-reminder templates, so this client sends a kind
plus the few values that fill the template, never free text.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("milatexai.mailer")

KINDS = ("payment_failed", "payment_reminder")
DEFAULT_URL = "https://status.milatexai.com/api/customer-email"


class CustomerMailer:
    def __init__(self, secret: str = "", url: str = DEFAULT_URL) -> None:
        self._secret = secret
        self.url = url
        self.enabled = bool(secret)

    @classmethod
    def from_env(cls) -> "CustomerMailer":
        return cls(os.environ.get("CUSTOMER_EMAIL_SECRET", ""), os.environ.get("CUSTOMER_EMAIL_URL", DEFAULT_URL))

    async def send(self, kind: str, to: str, pay_url: str, ends_on: str, amount: str) -> bool:
        """True when the email was accepted for delivery. Never raises."""
        if not self.enabled or kind not in KINDS or not to or not pay_url:
            return False
        try:
            import aiohttp

            body = {"kind": kind, "to": to, "pay_url": pay_url, "ends_on": ends_on, "amount": amount}
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self.url, json=body, headers={"Authorization": "Bearer " + self._secret,
                                                                       "User-Agent": "milatexai-app/1.0"}) as resp:
                    if resp.status == 200:
                        return True
                    log.warning("the email service refused a %s email (HTTP %s)", kind, resp.status)
                    return False
        except Exception as exc:  # noqa: BLE001
            log.warning("could not send a %s email (%s)", kind, type(exc).__name__)
            return False
