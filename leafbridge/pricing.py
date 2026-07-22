"""Pro-plan pricing.

The Pro plan is one simple price — **USD $8.99 / month** — everywhere. Stripe's
Adaptive Pricing presents the equivalent in the customer's local currency at
checkout (automatic FX), so researchers around the world see a familiar amount
while the plan stays a flat USD price with no per-currency bookkeeping.
"""

from __future__ import annotations

PRO_BASE_CURRENCY = "usd"
PRO_UNIT_AMOUNT = 899  # 8.99 in minor units (cents)
PRO_INTERVAL = "month"


def currency_options() -> dict[str, dict[str, int]]:
    """No fixed per-currency amounts — an empty map lets Stripe Adaptive Pricing
    convert the USD price into the customer's local currency at checkout."""
    return {}


def display_price() -> str:
    """The headline price shown on the site: $8.99 (USD)."""
    return f"${PRO_UNIT_AMOUNT / 100:.2f}"
