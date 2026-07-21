"""Tests for the Pro pricing: a flat USD $8.99 (Stripe converts to local currency
at checkout, so there are no per-currency overrides)."""

from __future__ import annotations

from leafbridge import pricing


def test_base_is_usd_899_monthly():
    assert pricing.PRO_BASE_CURRENCY == "usd"
    assert pricing.PRO_UNIT_AMOUNT == 899
    assert pricing.PRO_INTERVAL == "month"


def test_no_currency_options_so_stripe_converts():
    # Empty currency_options => Stripe Adaptive Pricing converts from USD.
    assert pricing.currency_options() == {}


def test_display_price():
    assert pricing.display_price() == "$8.99"
