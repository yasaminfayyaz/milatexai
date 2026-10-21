"""The first-month discount for new subscribers: who gets it, that checkout never breaks because
of it, and that the website and chat messages only promise it where it applies."""

from __future__ import annotations

import asyncio
import os
import types
import warnings

import pytest
from starlette.testclient import TestClient

from leafbridge import pricing, site
from leafbridge.billing import Billing
from leafbridge.service import AccountService, LimitExceeded
from leafbridge.store import InMemoryStore, TokenCipher, User

warnings.filterwarnings("ignore", category=DeprecationWarning)
os.environ.setdefault("WORKOS_AUTHKIT_DOMAIN", "https://placeholder.authkit.invalid")


class _Missing(Exception):
    code = "resource_missing"


class FakeStripe:
    """Just enough of the Stripe library to see what the app asks it to do."""

    def __init__(self, *, coupon_exists=False, past_subscriptions=0, coupon_create_fails=False, sub_list_fails=False):
        self.coupons = {"first-month-50": types.SimpleNamespace(id="first-month-50", valid=True)} if coupon_exists else {}
        self.created_coupons = []
        self.sessions = []
        self.customers = []
        outer = self

        class Coupon:
            @staticmethod
            def retrieve(cid):
                if cid not in outer.coupons:
                    raise _Missing()
                return outer.coupons[cid]

            @staticmethod
            def create(**kw):
                if coupon_create_fails:
                    raise PermissionError("this key may not create coupons")
                outer.created_coupons.append(kw)
                outer.coupons[kw["id"]] = types.SimpleNamespace(id=kw["id"], valid=True)
                return outer.coupons[kw["id"]]

        class Subscription:
            @staticmethod
            def list(**kw):
                if sub_list_fails:
                    raise ConnectionError("stripe is down")
                return types.SimpleNamespace(data=[{"id": f"sub_{i}"} for i in range(past_subscriptions)])

        class Customer:
            @staticmethod
            def create(**kw):
                outer.customers.append(kw)
                return types.SimpleNamespace(id="cus_new")

        class Session:
            @staticmethod
            def create(**kw):
                outer.sessions.append(kw)
                return types.SimpleNamespace(url="https://checkout.example/s")

        self.Coupon, self.Subscription, self.Customer = Coupon, Subscription, Customer
        self.checkout = types.SimpleNamespace(Session=Session)


def billing(fake):
    b = Billing(api_key="sk_test_x", price_id="price_x", webhook_secret="w", success_url="s", cancel_url="c", portal_return_url="p")
    b._stripe = lambda: fake
    return b


USER = User(user_id="u1", email="a@example.com")


def checkout(fake, customer_id=None):
    return asyncio.run(billing(fake).create_checkout(USER, customer_id))


def test_a_new_subscriber_gets_the_coupon_applied_automatically():
    fake = FakeStripe()
    url, cid = checkout(fake)
    assert url and cid == "cus_new"
    params = fake.sessions[0]
    assert params["discounts"] == [{"coupon": "first-month-50"}]
    assert "allow_promotion_codes" not in params           # Stripe does not accept both together
    assert fake.created_coupons == [{"id": "first-month-50", "percent_off": 50, "duration": "once",
                                     "name": "50% off your first month"}]


def test_the_coupon_is_created_once_and_then_reused():
    fake = FakeStripe()
    checkout(fake)
    checkout(fake)
    assert len(fake.created_coupons) == 1 and len(fake.sessions) == 2


def test_someone_who_ever_subscribed_gets_no_discount_but_keeps_the_promo_code_box():
    fake = FakeStripe(coupon_exists=True, past_subscriptions=1)
    checkout(fake, "cus_old")
    params = fake.sessions[0]
    assert "discounts" not in params and params["allow_promotion_codes"] is True


def test_a_customer_who_only_abandoned_a_checkout_still_counts_as_new():
    fake = FakeStripe(coupon_exists=True, past_subscriptions=0)
    checkout(fake, "cus_abandoned")
    assert fake.sessions[0]["discounts"] == [{"coupon": "first-month-50"}]


def test_when_the_offer_is_off_nobody_gets_it(monkeypatch):
    monkeypatch.setattr(pricing, "FIRST_MONTH_PERCENT", 0)
    fake = FakeStripe()
    checkout(fake)
    assert "discounts" not in fake.sessions[0] and fake.sessions[0]["allow_promotion_codes"] is True
    assert fake.created_coupons == []
    assert pricing.offer_suffix(False) == ""


def test_checkout_still_works_if_the_coupon_cannot_be_made():
    fake = FakeStripe(coupon_create_fails=True)
    url, _cid = checkout(fake)
    assert url == "https://checkout.example/s"
    assert "discounts" not in fake.sessions[0] and fake.sessions[0]["allow_promotion_codes"] is True


def test_checkout_still_works_if_the_past_subscription_check_fails():
    fake = FakeStripe(coupon_exists=True, sub_list_fails=True)
    url, _cid = checkout(fake, "cus_x")
    assert url and "discounts" not in fake.sessions[0]       # when in doubt, no discount


def test_prices_and_ids():
    assert pricing.first_month_price() == "$4.50" and pricing.first_month_price(100) == "$0.00"
    assert pricing.first_month_coupon_id() == "first-month-50" and pricing.first_month_coupon_id(30) == "first-month-30"


# --- messages --------------------------------------------------------------------------------

def test_the_limit_message_only_promises_the_discount_to_people_who_never_subscribed():
    svc = AccountService(InMemoryStore(), TokenCipher(TokenCipher.generate_key()))

    async def message(customer):
        user = await svc.get_or_create_user(f"u_{customer or 'new'}", "x@example.com")
        if customer:
            user.stripe_customer_id = customer
            await svc.store.upsert_user(user)
        await svc.store.increment_usage(user.user_id, "2026-10", 10)
        with pytest.raises(LimitExceeded) as e:
            await svc.check_commit_allowed(user.user_id, "2026-10")
        return str(e.value)

    new, returning = asyncio.run(message(None)), asyncio.run(message("cus_old"))
    assert "(50% off your first month)" in new and "/upgrade" in new
    assert "off your first month" not in returning and "/upgrade" in returning


def test_the_upgrade_tool_mentions_the_discount_only_when_it_applies():
    from fastmcp import Client

    from leafbridge.hosted import create_hosted_server

    class Fake:
        enabled = True

        def __init__(self, percent):
            self.percent = percent

        async def first_month_percent(self, customer_id):
            return self.percent

        async def create_checkout(self, user, customer_id=None):
            return "https://checkout.example/s", "cus_1"

    def run(percent):
        store = InMemoryStore()
        mcp = create_hosted_server(store=store, auth=False, billing=Fake(percent),
                                   identity_provider=lambda: ("u1", "a@example.com"))

        async def go():
            async with Client(mcp) as c:
                return (await c.call_tool("upgrade", {})).content[0].text
        return asyncio.run(go())

    assert "50% off (about $4.50)" in run(50)
    assert "off" not in run(0).split("https://checkout.example/s")[1]


# --- website ---------------------------------------------------------------------------------

def test_the_pricing_card_and_faq_mention_it_in_every_language():
    content = site.load_content()
    for lang, c in content.items():
        offer = c["pricing"]["pro"].get("offer", "")
        assert "50" in offer, f"{lang}: pricing.pro.offer missing or not 50%"
        assert "50" in c["faq"]["items"][1]["a"], f"{lang}: the cost FAQ does not mention it"
        assert chr(0x2014) not in offer and chr(0x2014) not in c["faq"]["items"][1]["a"]
    assert "50%" in content["en"]["pricing"]["pro"]["offer"] and str(pricing.FIRST_MONTH_PERCENT) == "50"
    page = site.render_site()
    assert "pricing.pro.offer" in page and "50% off your first month" in page


def test_the_account_page_offers_it_to_new_subscribers_only():
    acct = {"email": "a@example.com", "plan": "free", "has_customer": False}
    new = site.render_account(signed_in=True, account=acct, first_month_percent=50)
    returning = site.render_account(signed_in=True, account={**acct, "has_customer": True}, first_month_percent=0)
    assert "50% off their first month ($4.50)" in new
    assert "off their first month" not in returning


def test_the_upgrade_link_in_the_chat_messages_is_not_a_dead_end():
    from leafbridge.hosted import create_hosted_server

    mcp = create_hosted_server(store=InMemoryStore(), auth=False, identity_provider=lambda: ("u1", "a@example.com"))
    with TestClient(mcp.http_app()) as client:
        r = client.get("/upgrade", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/account"
