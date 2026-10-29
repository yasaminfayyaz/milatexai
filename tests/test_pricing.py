"""Tests for the Pro pricing: USD $8.99, and the same number in CAD and AUD."""

from __future__ import annotations

from leafbridge import pricing


def test_base_is_usd_899_monthly():
    assert pricing.PRO_BASE_CURRENCY == "usd"
    assert pricing.PRO_UNIT_AMOUNT == 899
    assert pricing.PRO_INTERVAL == "month"


def test_canada_and_australia_pay_the_same_number_in_their_own_currency():
    assert pricing.currency_options() == {"cad": {"unit_amount": 899}, "aud": {"unit_amount": 899}}


def test_local_amounts_follow_whatever_the_base_price_is():
    assert pricing.currency_options(699) == {"cad": {"unit_amount": 699}, "aud": {"unit_amount": 699}}


def test_display_price():
    assert pricing.display_price() == "$8.99"
