"""Idempotent Stripe setup for MiLatexAI.

Creates (once) the Pro product, the localized recurring Price (USD $4.99 base +
currency_options for the strong currencies, per leafbridge.pricing), and the
webhook endpoint that keeps plans in sync. Writes STRIPE_PRICE_ID and
STRIPE_WEBHOOK_SECRET into .env if they aren't already there.

    python ops/stripe_setup.py

Re-runnable: an existing Price (matched by lookup_key) and webhook (matched by
URL) are reused. NOTE: Stripe only returns a webhook's signing secret at creation
time, so if the endpoint already exists this script cannot recover the secret —
delete it in the dashboard and re-run to mint a fresh one.
"""

from __future__ import annotations

import os
import sys

import stripe
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from leafbridge import pricing  # noqa: E402

ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
PRICE_LOOKUP_KEY = "milatexai_pro_monthly"
WEBHOOK_URL = "https://milatexai.com/stripe/webhook"
WEBHOOK_EVENTS = [
    "checkout.session.completed",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.payment_failed",
]


def _upsert_env(key: str, value: str) -> bool:
    """Append KEY=value to .env if KEY isn't already present. Returns True if added."""
    existing = ""
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            existing = f.read()
    if any(line.strip().startswith(f"{key}=") for line in existing.splitlines()):
        return False
    with open(ENV_PATH, "a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(f"{key}={value}\n")
    return True


def ensure_price():
    found = stripe.Price.list(lookup_keys=[PRICE_LOOKUP_KEY], limit=1, expand=["data.product"])
    if found.data:
        return found.data[0], False
    product = stripe.Product.create(
        name="MiLatexAI Pro",
        description="Unlimited Overleaf projects and unlimited write-commits.",
    )
    price = stripe.Price.create(
        product=product.id,
        currency=pricing.PRO_BASE_CURRENCY,
        unit_amount=pricing.PRO_UNIT_AMOUNT,
        recurring={"interval": pricing.PRO_INTERVAL},
        currency_options=pricing.currency_options(),
        lookup_key=PRICE_LOOKUP_KEY,
    )
    return price, True


def ensure_webhook():
    for ep in stripe.WebhookEndpoint.list(limit=100).auto_paging_iter():
        if ep.url == WEBHOOK_URL:
            return ep, None  # exists; secret can't be re-fetched
    ep = stripe.WebhookEndpoint.create(
        url=WEBHOOK_URL,
        enabled_events=WEBHOOK_EVENTS,
        description="MiLatexAI plan sync",
    )
    return ep, ep.secret


def main() -> None:
    load_dotenv(dotenv_path=ENV_PATH)
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

    price, price_created = ensure_price()
    print(f"PRICE: {price.id}  ({'created' if price_created else 'reused'})  "
          f"base {price.currency} {price.unit_amount}")
    added_price = _upsert_env("STRIPE_PRICE_ID", price.id)

    ep, secret = ensure_webhook()
    print(f"WEBHOOK: {ep.id} -> {ep.url}  ({'created' if secret else 'already existed'})")
    added_secret = False
    if secret:
        added_secret = _upsert_env("STRIPE_WEBHOOK_SECRET", secret)
    elif not any(
        l.startswith("STRIPE_WEBHOOK_SECRET=")
        for l in (open(ENV_PATH, encoding="utf-8").read().splitlines() if os.path.exists(ENV_PATH) else [])
    ):
        print("  WARNING: endpoint already existed and STRIPE_WEBHOOK_SECRET is not in .env. "
              "Delete the endpoint in the Stripe dashboard and re-run to mint a fresh secret.")

    print(f".env updates -> STRIPE_PRICE_ID added: {added_price} ; "
          f"STRIPE_WEBHOOK_SECRET added: {added_secret}")


if __name__ == "__main__":
    main()
