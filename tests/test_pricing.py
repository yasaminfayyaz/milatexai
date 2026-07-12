"""Tests for the localized Pro pricing rule (4.99 local where >= 4.99 CAD, else
4.99 USD)."""

from __future__ import annotations

from leafbridge import pricing


def test_base_is_usd_499():
    assert pricing.PRO_BASE_CURRENCY == "usd"
    assert pricing.PRO_UNIT_AMOUNT == 499


def test_strong_currencies_bill_locally():
    for code in ("eur", "gbp", "chf", "cad", "sgd", "usd"):
        cur, amt = pricing.price_for_currency(code)
        assert cur == code and amt == 499


def test_weak_currencies_fall_back_to_usd():
    # Currencies weaker than the CAD must bill at USD 4.99, not 4.99 local.
    for code in ("jpy", "inr", "brl", "mxn", "cny", "krw", "rub", "aud", "nzd"):
        cur, amt = pricing.price_for_currency(code)
        assert cur == "usd" and amt == 499, f"{code} should fall back to USD"


def test_currency_options_excludes_base_and_lists_strong():
    opts = pricing.currency_options()
    assert "usd" not in opts  # Stripe rejects a currency_option matching the base
    for code in ("eur", "gbp", "chf", "cad", "sgd"):
        assert opts[code] == {"unit_amount": 499}


def test_display_price_symbols():
    assert pricing.display_price("usd") == "$4.99"
    assert pricing.display_price("eur") == "€4.99"
    assert pricing.display_price("gbp") == "£4.99"
    # unknown/weak currency shows the USD fallback price
    assert pricing.display_price("jpy") == "$4.99"


def test_case_insensitive():
    assert pricing.price_for_currency("EUR") == ("eur", 499)
