"""Pro-plan pricing, including the localized-currency rule.

Business rule (set by the owner): the Pro plan costs **4.99 in the customer's
local currency wherever 4.99 of that currency is worth at least 4.99 CAD**, and
**4.99 USD everywhere else**.

Because the 4.99 cancels on both sides, this is equivalent to: *use the local
currency iff one unit of it is worth at least one CAD* (i.e. the currency is at
least as strong as the Canadian dollar). Weaker currencies (JPY, INR, BRL, …)
would make "4.99 local" cheaper than the 4.99 CAD floor, so they fall back to
4.99 USD.

We implement this with a single Stripe recurring Price whose **base currency is
USD ($4.99)** plus ``currency_options`` at 4.99 for each qualifying strong
currency. Any currency not listed automatically bills at the USD base — exactly
the intended fallback — so we never have to enumerate the weak currencies.

All amounts here are in the currency's minor unit (cents). Every qualifying
currency is a 2-decimal currency, so 4.99 == 499 across the board (no zero-decimal
special-casing needed, since no zero-decimal currency is stronger than the CAD).
"""

from __future__ import annotations

PRO_BASE_CURRENCY = "usd"
PRO_UNIT_AMOUNT = 499  # 4.99 in minor units (cents)
PRO_INTERVAL = "month"

# Currencies at least as strong as the CAD (1 unit >= ~1 CAD), which therefore
# bill at 4.99 in their own currency. Everything else falls back to USD $4.99.
# Symbols are for display on the marketing site.
QUALIFYING_CURRENCIES: dict[str, str] = {
    "usd": "$",   # base
    "eur": "€",
    "gbp": "£",
    "chf": "CHF ",
    "cad": "CA$",
    "sgd": "S$",
}


def currency_options() -> dict[str, dict[str, int]]:
    """The ``currency_options`` payload for creating the Stripe Price.

    Excludes the base currency (USD) — Stripe rejects a currency_option that
    duplicates the Price's own currency.
    """
    return {
        code: {"unit_amount": PRO_UNIT_AMOUNT}
        for code in QUALIFYING_CURRENCIES
        if code != PRO_BASE_CURRENCY
    }


def price_for_currency(currency: str) -> tuple[str, int]:
    """Return ``(currency, unit_amount)`` that a customer in ``currency`` is
    charged: their own currency if it qualifies, otherwise the USD base."""
    code = (currency or "").lower()
    if code in QUALIFYING_CURRENCIES:
        return code, PRO_UNIT_AMOUNT
    return PRO_BASE_CURRENCY, PRO_UNIT_AMOUNT


def display_price(currency: str = "usd") -> str:
    """A human string like ``$4.99`` / ``€4.99`` for the pricing card."""
    code, amount = price_for_currency(currency)
    symbol = QUALIFYING_CURRENCIES.get(code, "$")
    return f"{symbol}{amount / 100:.2f}"
