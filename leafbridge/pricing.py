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


# Invite a friend. The friend, arriving through a Pro subscriber's link, gets a bigger first-month
# discount than the standard one (that is the reason to use the link). The inviter gets this much off
# one month, as credit on their Stripe balance, for each friend who pays, up to the cap. The reward
# is held a few days after the friend's first payment so a refund cancels it. 0 on any of the first
# three switches the program off everywhere.
REFERRAL_FRIEND_PERCENT = 75
REFERRAL_REWARD_PERCENT = 50
REFERRAL_MAX_REWARDS = 12
REFERRAL_HOLD_DAYS = 7


def referral_enabled() -> bool:
    return bool(FIRST_MONTH_PERCENT and REFERRAL_FRIEND_PERCENT and REFERRAL_REWARD_PERCENT and REFERRAL_MAX_REWARDS)


def referral_credit_cents() -> int:
    """What one reward is worth: the reward percentage of a month, in cents ($4.50)."""
    return (PRO_UNIT_AMOUNT * REFERRAL_REWARD_PERCENT + 50) // 100


def offer_percent(referred: bool) -> int:
    """The first-month discount for someone who never subscribed: the friend rate when they came
    through an invite link, otherwise the standard one."""
    if referred and referral_enabled():
        return REFERRAL_FRIEND_PERCENT
    return FIRST_MONTH_PERCENT


def offer_suffix(has_customer: bool, referred: bool = False) -> str:
    """" (50% off your first month)" for someone who has never subscribed (no Stripe customer
    yet), otherwise nothing. Used in the messages that point people to the upgrade page."""
    if not FIRST_MONTH_PERCENT or has_customer:
        return ""
    return f" ({offer_percent(referred)}% off your first month)"


def display_price() -> str:
    """The headline price shown on the site: $8.99 (USD)."""
    return f"${PRO_UNIT_AMOUNT / 100:.2f}"
