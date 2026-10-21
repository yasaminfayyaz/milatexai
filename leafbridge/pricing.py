"""Pro-plan pricing.

The Pro plan is one simple price of **USD $8.99 / month** everywhere. Stripe's
Adaptive Pricing presents the equivalent in the customer's local currency at
checkout (automatic FX), so researchers around the world see a familiar amount
while the plan stays a flat USD price with no per-currency bookkeeping.
"""

from __future__ import annotations

PRO_BASE_CURRENCY = "usd"
PRO_UNIT_AMOUNT = 899  # 8.99 in minor units (cents)
PRO_INTERVAL = "month"


def currency_options() -> dict[str, dict[str, int]]:
    """No fixed per-currency amounts: an empty map lets Stripe Adaptive Pricing
    convert the USD price into the customer's local currency at checkout."""
    return {}


# Offer for new subscribers: this much off the FIRST month, taken off automatically at checkout
# through a Stripe coupon (see billing.py). Someone who has ever had a subscription does not get it
# again. 0 switches the offer off everywhere: checkout, the website and the chat messages.
FIRST_MONTH_PERCENT = 50


def first_month_coupon_id(percent: int | None = None) -> str:
    """The Stripe coupon behind the offer. The percentage is part of the id, so changing it
    makes a new coupon instead of editing one that past customers already used."""
    return f"first-month-{FIRST_MONTH_PERCENT if percent is None else percent}"


def first_month_price(percent: int | None = None) -> str:
    """What the first month costs, in USD (Stripe shows the local currency at checkout)."""
    pct = FIRST_MONTH_PERCENT if percent is None else percent
    cents = (PRO_UNIT_AMOUNT * (100 - pct) + 50) // 100
    return f"${cents / 100:.2f}"


def offer_suffix(has_customer: bool) -> str:
    """" (50% off your first month)" for someone who has never subscribed (no Stripe customer
    yet), otherwise nothing. Used in the messages that point people to the upgrade page."""
    if not FIRST_MONTH_PERCENT or has_customer:
        return ""
    return f" ({FIRST_MONTH_PERCENT}% off your first month)"


def display_price() -> str:
    """The headline price shown on the site: $8.99 (USD)."""
    return f"${PRO_UNIT_AMOUNT / 100:.2f}"
