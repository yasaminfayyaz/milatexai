"""Canada and Australia pay the same number in their own currency: the Stripe price gets CAD and AUD
amounts copied from its own live amount, kept up to date, and never able to break a checkout."""

from __future__ import annotations

import asyncio
import os
import types

os.environ.setdefault("LEAFBRIDGE_BASE_URL", "https://example.test")
os.environ.setdefault("WORKOS_AUTHKIT_DOMAIN", "https://placeholder.authkit.invalid")

from leafbridge import pricing  # noqa: E402
from leafbridge.billing import Billing  # noqa: E402
from leafbridge.store import User  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class FakeStripe:
    """A Stripe price that remembers the currency amounts it is given."""

    def __init__(self, unit_amount=899, options=None, retrieve_fails=False, modify_fails=False):
        self.unit_amount = unit_amount
        self.options = {"usd": {"unit_amount": unit_amount}, **(options or {})}
        self.retrieves = 0
        self.modifies: list[dict] = []
        self.sessions: list[dict] = []
        outer = self

        class Price:
            @staticmethod
            def retrieve(pid, **kw):
                outer.retrieves += 1
                if retrieve_fails:
                    raise ConnectionError("stripe is down")
                return types.SimpleNamespace(unit_amount=outer.unit_amount, currency="usd",
                                             currency_options=dict(outer.options))

            @staticmethod
            def modify(pid, **kw):
                if modify_fails:
                    raise PermissionError("this key may not edit prices")
                outer.modifies.append(kw["currency_options"])
                outer.options.update(kw["currency_options"])

        class Subscription:
            @staticmethod
            def list(**kw):
                return types.SimpleNamespace(data=[])

        class Customer:
            @staticmethod
            def create(**kw):
                return types.SimpleNamespace(id="cus_new")

        class Coupon:
            @staticmethod
            def retrieve(cid):
                return types.SimpleNamespace(id=cid, valid=True)

        class Session:
            @staticmethod
            def create(**kw):
                outer.sessions.append(kw)
                return types.SimpleNamespace(url="https://checkout.example/s")

        self.Price, self.Subscription, self.Customer, self.Coupon = Price, Subscription, Customer, Coupon
        self.checkout = types.SimpleNamespace(Session=Session)


def billing(fake):
    b = Billing(api_key="sk_test_x", price_id="price_x", webhook_secret="w", success_url="s", cancel_url="c",
                portal_return_url="p")
    b._stripe = lambda: fake
    return b


def checkout(b):
    return run(b.create_checkout(User(user_id="u", email="u@example.com"), None))


def test_the_first_checkout_adds_cad_and_aud_at_the_same_number():
    fake = FakeStripe()
    checkout(billing(fake))
    assert fake.modifies == [{"cad": {"unit_amount": 899}, "aud": {"unit_amount": 899}}]
    assert len(fake.sessions) == 1                                       # and the checkout itself went ahead


def test_nothing_is_written_when_the_price_is_already_right():
    fake = FakeStripe(options={"cad": {"unit_amount": 899}, "aud": {"unit_amount": 899}})
    checkout(billing(fake))
    assert fake.modifies == []


def test_a_new_base_price_carries_over_by_itself():
    fake = FakeStripe(unit_amount=699)                                   # a later price cut
    checkout(billing(fake))
    assert fake.modifies == [{"cad": {"unit_amount": 699}, "aud": {"unit_amount": 699}}]


def test_only_the_wrong_currency_is_rewritten():
    fake = FakeStripe(options={"cad": {"unit_amount": 899}, "aud": {"unit_amount": 1299}})
    checkout(billing(fake))
    assert fake.modifies == [{"aud": {"unit_amount": 899}}]


def test_it_is_checked_once_an_hour_not_on_every_checkout():
    fake = FakeStripe()
    b = billing(fake)
    checkout(b)
    checkout(b)
    checkout(b)
    assert fake.retrieves == 1 and len(fake.modifies) == 1 and len(fake.sessions) == 3


def test_if_stripe_cannot_be_read_the_checkout_still_works_in_usd():
    fake = FakeStripe(retrieve_fails=True)
    assert checkout(billing(fake))[0] == "https://checkout.example/s"
    assert fake.modifies == [] and len(fake.sessions) == 1


def test_if_the_key_may_not_edit_prices_the_checkout_still_works():
    fake = FakeStripe(modify_fails=True)
    assert checkout(billing(fake))[0] == "https://checkout.example/s"
    assert len(fake.sessions) == 1


def test_a_failed_check_is_retried_soon_not_in_an_hour():
    fake = FakeStripe(retrieve_fails=True)
    b = billing(fake)
    checkout(b)
    assert 0 < b._local_prices_check_after - __import__("time").monotonic() <= 600


def test_the_checkout_keeps_its_usd_price_and_discount():
    fake = FakeStripe()
    checkout(billing(fake))
    s = fake.sessions[0]
    assert s["line_items"] == [{"price": "price_x", "quantity": 1}] and s["discounts"] == [{"coupon": "first-month-50"}]
    assert "currency" not in s                    # Stripe picks CAD, AUD or USD from where the customer is


def test_the_currency_list_is_just_canada_and_australia():
    assert set(pricing.LOCAL_PRICE_CURRENCIES) == {"cad", "aud"}
