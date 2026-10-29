"""Invite a friend: the friend gets 75% off the first month, the inviter gets 50% off a month for each
friend who pays (up to 12), after a hold, never for themselves, never for a refunded first payment.
"""

from __future__ import annotations

import asyncio
import json
import time
import types
import warnings

import pytest
from fastmcp import Client
from starlette.testclient import TestClient

from leafbridge import pricing, referrals
from leafbridge.billing import Billing, first_payment_from_event
from leafbridge.dunning import DunningLoop
from leafbridge.hosted import REF_COOKIE, create_hosted_server
from leafbridge.service import AccountService
from leafbridge.store import InMemoryStore, TokenCipher, User
from leafbridge.web_session import mint_session

warnings.filterwarnings("ignore", category=DeprecationWarning)
DAY = 86400
GOOD = "0123456789abcdef01234567"


def run(coro):
    return asyncio.run(coro)


class FakeBilling:
    """Stands in for Stripe: records credits, answers the first-payment check."""

    enabled = True

    def __init__(self, state="ok", fingerprints=("fp_friend",), saved_cards=()):
        self.state, self.fingerprints, self.saved_cards = state, list(fingerprints), list(saved_cards)
        self.credits: list[tuple[str, int, str]] = []
        self.credit_ok = True
        self.checked = 0

    async def first_payment_check(self, customer_id):
        self.checked += 1
        return {"state": self.state, "fingerprints": self.fingerprints}

    async def card_fingerprints(self, customer_id):
        return self.saved_cards

    async def add_credit(self, customer_id, cents, key, note):
        if not self.credit_ok:
            return False
        if all(k != key for _, _, k in self.credits):           # Stripe ignores a repeated idempotency key
            self.credits.append((customer_id, cents, key))
        return True

    async def subscription_state(self, customer_id):
        return {"state": "active"}


def make(store, user_id, email, **kw):
    u = User(user_id=user_id, email=email, **kw)
    run(store.upsert_user(u))
    return u


def world(inviter_plan="pro", **inviter_kw):
    """An inviter with a code and a friend already linked to them, who has paid."""
    store = InMemoryStore()
    inviter = make(store, "inv", "inviter@example.com", plan=inviter_plan, stripe_customer_id="cus_inv", **inviter_kw)
    code = run(referrals.ensure_code(store, inviter))
    friend = make(store, "fr", "friend@example.com", plan="pro", stripe_customer_id="cus_fr")
    assert run(referrals.attach(store, friend, code))
    return store, code


def pay(store, at):
    run(referrals.on_first_payment(store, "cus_fr", at))


# --- the numbers -----------------------------------------------------------------------------------

def test_the_offer_numbers():
    assert pricing.REFERRAL_FRIEND_PERCENT == 75 and pricing.REFERRAL_REWARD_PERCENT == 50
    assert pricing.REFERRAL_MAX_REWARDS == 12
    assert pricing.first_month_price(75) == "$2.25"
    assert pricing.referral_credit_cents() == 450
    assert pricing.offer_percent(True) == 75 and pricing.offer_percent(False) == 50
    assert "(75% off your first month)" in pricing.offer_suffix(False, True)
    assert "(50% off your first month)" in pricing.offer_suffix(False)
    assert pricing.offer_suffix(True, True) == ""


def test_the_program_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(pricing, "REFERRAL_MAX_REWARDS", 0)
    assert not pricing.referral_enabled() and pricing.offer_percent(True) == 50
    store = InMemoryStore()
    inviter = make(store, "inv", "i@example.com")
    code = run(referrals.ensure_code(store, inviter))
    friend = make(store, "fr", "f@example.com")
    assert not run(referrals.attach(store, friend, code))


# --- linking a friend to the inviter -----------------------------------------------------------------

def test_codes_are_short_unique_and_stable():
    store = InMemoryStore()
    a, b = make(store, "a", "a@example.com"), make(store, "b", "b@example.com")
    ca, cb = run(referrals.ensure_code(store, a)), run(referrals.ensure_code(store, b))
    assert referrals.CODE_RE.match(ca) and ca != cb
    assert run(referrals.ensure_code(store, a)) == ca
    assert referrals.link("https://milatexai.com/", ca) == f"https://milatexai.com/r/{ca}"


def test_email_spellings_of_one_mailbox_match():
    n = referrals.normalize_email
    assert n("Jane.Doe+thesis@Gmail.com") == n("janedoe@googlemail.com") == "janedoe@gmail.com"
    assert n("x+y@uni.edu") == "x@uni.edu" and n("x.y@uni.edu") == "x.y@uni.edu"


def test_attaching_works_once_and_refuses_yourself_and_junk():
    store = InMemoryStore()
    inviter = make(store, "inv", "inviter@gmail.com")
    code = run(referrals.ensure_code(store, inviter))
    friend = make(store, "fr", "friend@example.com")
    assert run(referrals.attach(store, friend, code))
    assert run(store.get_user("fr")).referred_by == "inv"
    assert not run(referrals.attach(store, friend, code))                     # only once
    other = make(store, "o", "o@example.com")
    assert not run(referrals.attach(store, other, "deadbeef"))               # no such code
    assert not run(referrals.attach(store, other, "../../etc"))              # not even shaped like a code
    assert not run(referrals.attach(store, inviter, code))                   # yourself
    twin = make(store, "twin", "In.Viter+2@gmail.com")
    assert not run(referrals.attach(store, twin, code))                      # your other spelling of the same mailbox
    admin = make(store, "adm", "adm@example.com", is_admin=True)
    assert not run(referrals.attach(store, admin, code))


# --- the reward --------------------------------------------------------------------------------------

def test_nothing_happens_until_the_hold_has_passed():
    store, _ = world()
    now = time.time()
    pay(store, now)
    f = FakeBilling()
    assert run(referrals.sweep(store, f, now + 6 * DAY))["rewarded"] == 0
    assert f.credits == [] and f.checked == 0
    report = run(referrals.sweep(store, f, now + 7 * DAY + 1))
    assert report["rewarded"] == 1 and report["applied"] == 1
    assert f.credits == [("cus_inv", 450, "referral-credit-inv-1")]
    inv = run(store.get_user("inv"))
    assert (inv.referral_rewards, inv.referral_pending) == (1, 0)
    assert run(store.get_user("fr")).referral_done


def test_a_second_sweep_never_rewards_twice():
    store, _ = world()
    now = time.time()
    pay(store, now)
    f = FakeBilling()
    later = now + 8 * DAY
    run(referrals.sweep(store, f, later))
    run(referrals.sweep(store, f, later + 3600))
    run(referrals.sweep(store, f, later + 2 * 3600))
    assert len(f.credits) == 1 and run(store.get_user("inv")).referral_rewards == 1


def test_the_first_payment_is_noted_once_and_only_for_invited_friends():
    store, _ = world()
    assert run(referrals.on_first_payment(store, "cus_fr", 1000.0)) == "noted"
    assert run(referrals.on_first_payment(store, "cus_fr", 2000.0)) == "ignored"
    assert run(store.get_user("fr")).referral_paid_at == 1000.0
    assert run(referrals.on_first_payment(store, "cus_inv", 1.0)) == "ignored"       # not invited by anyone
    assert run(referrals.on_first_payment(store, "cus_nobody", 1.0)) == "ignored"


def test_a_refunded_first_payment_earns_nothing():
    store, _ = world()
    now = time.time()
    pay(store, now)
    f = FakeBilling(state="refunded")
    report = run(referrals.sweep(store, f, now + 9 * DAY))
    assert report["skipped"] == 1 and f.credits == []
    assert run(store.get_user("fr")).referral_done and run(store.get_user("inv")).referral_rewards == 0


def test_paying_with_the_inviters_own_card_earns_nothing():
    store, _ = world()
    now = time.time()
    pay(store, now)
    f = FakeBilling(fingerprints=["fp_same"], saved_cards=["fp_other", "fp_same"])
    assert run(referrals.sweep(store, f, now + 9 * DAY))["skipped"] == 1 and f.credits == []


def test_when_stripe_cannot_say_the_decision_waits_and_is_retried():
    store, _ = world()
    now = time.time()
    pay(store, now)
    f = FakeBilling(state="unknown")
    assert run(referrals.sweep(store, f, now + 9 * DAY))["waiting"] == 1
    assert not run(store.get_user("fr")).referral_done
    f.state = "ok"
    assert run(referrals.sweep(store, f, now + 9 * DAY + 21600))["rewarded"] == 1


def test_at_most_twelve_rewards_per_person():
    store, _ = world(referral_rewards=pricing.REFERRAL_MAX_REWARDS)
    now = time.time()
    pay(store, now)
    f = FakeBilling()
    report = run(referrals.sweep(store, f, now + 9 * DAY))
    assert report["skipped"] == 1 and f.credits == []
    assert run(store.get_user("inv")).referral_rewards == 12


def test_the_twelfth_reward_is_paid_and_the_thirteenth_is_not():
    store = InMemoryStore()
    inviter = make(store, "inv", "inviter@example.com", plan="pro", stripe_customer_id="cus_inv")
    code = run(referrals.ensure_code(store, inviter))
    f = FakeBilling()
    now = time.time()
    for i in range(13):
        friend = make(store, f"f{i}", f"friend{i}@example.com", plan="pro", stripe_customer_id=f"cus_f{i}")
        assert run(referrals.attach(store, friend, code))
        run(referrals.on_first_payment(store, f"cus_f{i}", now))
    run(referrals.sweep(store, f, now + 9 * DAY))
    assert len(f.credits) == 12
    assert sorted(k for _, _, k in f.credits) == sorted(f"referral-credit-inv-{n}" for n in range(1, 13))
    inv = run(store.get_user("inv"))
    assert (inv.referral_rewards, inv.referral_pending) == (12, 0)


def test_a_free_inviter_keeps_the_reward_until_they_are_pro():
    store, _ = world(inviter_plan="free")
    now = time.time()
    pay(store, now)
    f = FakeBilling()
    run(referrals.sweep(store, f, now + 9 * DAY))
    inv = run(store.get_user("inv"))
    assert (inv.referral_rewards, inv.referral_pending) == (1, 1) and f.credits == []
    run(referrals.sweep(store, f, now + 10 * DAY))                        # still free: still waiting
    assert f.credits == []
    inv.plan = "pro"
    run(store.upsert_user(inv))
    assert run(referrals.apply_pending(store, f, "inv")) == 1
    assert f.credits == [("cus_inv", 450, "referral-credit-inv-1")]
    assert run(store.get_user("inv")).referral_pending == 0
    assert run(referrals.apply_pending(store, f, "inv")) == 0             # nothing left to apply


def test_a_credit_that_stripe_refuses_stays_waiting_and_is_applied_later():
    store, _ = world()
    now = time.time()
    pay(store, now)
    f = FakeBilling()
    f.credit_ok = False
    run(referrals.sweep(store, f, now + 9 * DAY))
    assert run(store.get_user("inv")).referral_pending == 1 and f.credits == []
    f.credit_ok = True
    assert run(referrals.sweep(store, f, now + 10 * DAY))["applied"] == 1
    assert len(f.credits) == 1 and run(store.get_user("inv")).referral_pending == 0


def test_several_waiting_rewards_get_distinct_numbers():
    store, _ = world(inviter_plan="free", referral_rewards=3, referral_pending=3)
    inv = run(store.get_user("inv"))
    inv.plan = "pro"
    run(store.upsert_user(inv))
    f = FakeBilling()
    assert run(referrals.apply_pending(store, f, "inv")) == 3
    assert [k for _, _, k in f.credits] == [f"referral-credit-inv-{n}" for n in (1, 2, 3)]


def test_the_payment_sweep_runs_the_referral_sweep_too():
    store, _ = world()
    now = time.time()
    pay(store, now)
    f = FakeBilling()
    loop = DunningLoop(store, f, types.SimpleNamespace(enabled=False), clock=lambda: now + 9 * DAY)
    report = run(loop.tick())
    assert report["referrals"]["rewarded"] == 1 and len(f.credits) == 1


# --- Stripe events and the checkout --------------------------------------------------------------------

def test_only_a_real_first_payment_counts():
    def event(**obj):
        return {"type": "invoice.paid", "created": 1700000000, "data": {"object": {"customer": "cus_1", **obj}}}
    ok = first_payment_from_event(event(billing_reason="subscription_create", amount_paid=899))
    assert ok == {"customer": "cus_1", "at": 1700000000.0}
    assert first_payment_from_event(event(billing_reason="subscription_cycle", amount_paid=899)) is None
    assert first_payment_from_event(event(billing_reason="subscription_create", amount_paid=0)) is None
    assert first_payment_from_event({**event(billing_reason="subscription_create", amount_paid=899),
                                     "type": "invoice.payment_failed"}) is None


class _Missing(Exception):
    code = "resource_missing"


class FakeStripe:
    def __init__(self, past_subscriptions=0):
        self.coupons, self.created, self.sessions = {}, [], []
        outer = self

        class Coupon:
            @staticmethod
            def retrieve(cid):
                if cid not in outer.coupons:
                    raise _Missing()
                return outer.coupons[cid]

            @staticmethod
            def create(**kw):
                outer.created.append(kw)
                outer.coupons[kw["id"]] = types.SimpleNamespace(id=kw["id"], valid=True)
                return outer.coupons[kw["id"]]

        class Subscription:
            @staticmethod
            def list(**kw):
                return types.SimpleNamespace(data=[{"id": "s"}] * past_subscriptions)

        class Customer:
            @staticmethod
            def create(**kw):
                return types.SimpleNamespace(id="cus_new")

        class Session:
            @staticmethod
            def create(**kw):
                outer.sessions.append(kw)
                return types.SimpleNamespace(url="https://checkout.example/s")

        self.Coupon, self.Subscription, self.Customer = Coupon, Subscription, Customer
        self.checkout = types.SimpleNamespace(Session=Session)


def stripe_billing(fake):
    b = Billing(api_key="sk_test_x", price_id="price_x", webhook_secret="w", success_url="s", cancel_url="c",
                portal_return_url="p")
    b._stripe = lambda: fake
    return b


def test_an_invited_friend_gets_75_percent_and_anyone_else_50():
    fake = FakeStripe()
    run(stripe_billing(fake).create_checkout(User(user_id="a", email="a@x.com", referred_by="inv")))
    run(stripe_billing(fake).create_checkout(User(user_id="b", email="b@x.com")))
    assert fake.sessions[0]["discounts"] == [{"coupon": "first-month-75"}]
    assert fake.sessions[1]["discounts"] == [{"coupon": "first-month-50"}]
    assert {c["id"]: c["percent_off"] for c in fake.created} == {"first-month-75": 75, "first-month-50": 50}


def test_an_invite_does_not_help_someone_who_already_subscribed_once():
    fake = FakeStripe(past_subscriptions=1)
    run(stripe_billing(fake).create_checkout(User(user_id="a", email="a@x.com", referred_by="inv"), "cus_old"))
    assert "discounts" not in fake.sessions[0] and fake.sessions[0]["allow_promotion_codes"] is True


def test_the_stripe_calls_for_the_credit(monkeypatch):
    calls = []

    class Customer:
        @staticmethod
        def create_balance_transaction(cid, **kw):
            calls.append((cid, kw))

    fake = types.SimpleNamespace(Customer=Customer)
    assert run(stripe_billing(fake).add_credit("cus_1", 450, "key-1", "Invite a friend"))
    assert calls == [("cus_1", {"amount": -450, "currency": "usd", "description": "Invite a friend",
                                "idempotency_key": "key-1"})]

    class Broken:
        @staticmethod
        def create_balance_transaction(cid, **kw):
            raise ConnectionError("down")

    assert not run(stripe_billing(types.SimpleNamespace(Customer=Broken)).add_credit("cus_1", 450, "k", "n"))


def test_the_first_payment_check_reads_refunds_and_card_fingerprints():
    def charge(**kw):
        base = dict(paid=True, created=100, refunded=False, amount_refunded=0, disputed=False,
                    payment_method_details={"card": {"fingerprint": "fp1"}})
        return types.SimpleNamespace(**{**base, **kw})

    def check(charges):
        Charge = types.SimpleNamespace(list=lambda **kw: types.SimpleNamespace(data=charges))
        return run(stripe_billing(types.SimpleNamespace(Charge=Charge)).first_payment_check("cus"))

    assert check([charge()]) == {"state": "ok", "fingerprints": ["fp1"]}
    assert check([charge(amount_refunded=899)])["state"] == "refunded"
    assert check([charge(refunded=True)])["state"] == "refunded"
    assert check([charge(disputed=True)])["state"] == "refunded"
    assert check([charge(created=200, refunded=True), charge(created=100)])["state"] == "ok"   # the FIRST charge counts
    assert check([charge(paid=False)])["state"] == "unknown" and check([])["state"] == "unknown"


# --- the website and the tools -------------------------------------------------------------------------

def server(store=None, billing=None):
    store = store or InMemoryStore()
    cipher = TokenCipher(TokenCipher.generate_key())
    mcp = create_hosted_server(store=store, cipher=cipher, auth=False, billing=billing or FakeBilling(),
                               identity_provider=lambda: ("fr", "friend@example.com"), base_url="https://milatexai.com")
    return store, cipher, mcp


def client(mcp):
    return TestClient(mcp.http_app(), base_url="https://testserver")


def test_the_invite_link_sets_a_cookie_and_explains_the_offer():
    store, _, mcp = server()
    inviter = make(store, "inv", "inviter@example.com", plan="pro")
    code = run(referrals.ensure_code(store, inviter))
    with client(mcp) as c:
        r = c.get(f"/r/{code}", follow_redirects=False)
        assert r.status_code == 200 and "75% off" in r.text and "$2.25" in r.text
        assert c.cookies.get(REF_COOKIE) == code
        assert "inviter@example.com" not in r.text                           # who invited you is not revealed
        assert "no-store" in r.headers["cache-control"]


@pytest.mark.parametrize("code", ["deadbeef", "NOTACODE", "12345678901234567890", "..%2f"])
def test_an_unknown_invite_code_just_goes_home_with_no_cookie(code):
    _, _, mcp = server()
    with client(mcp) as c:
        r = c.get(f"/r/{code}", follow_redirects=False)
        assert r.status_code in (303, 307, 404) and not c.cookies.get(REF_COOKIE)
        if r.status_code == 303:
            assert r.headers["location"] == "/"


def test_connecting_after_an_invite_link_links_the_friend():
    from leafbridge.connect_link import mint_connect_code

    store, cipher, mcp = server()
    inviter = make(store, "inv", "inviter@example.com", plan="pro")
    code = run(referrals.ensure_code(store, inviter))
    with client(mcp) as c:
        c.get(f"/r/{code}")
        page = c.get("/connect", params={"code": mint_connect_code(cipher, "fr", "friend@example.com")})
        assert page.status_code == 200
        assert run(store.get_user("fr")).referred_by == "inv"
        done = c.post("/connect", data={"code": mint_connect_code(cipher, "fr", "friend@example.com"),
                                        "overleaf_url": f"https://www.overleaf.com/project/{GOOD}",
                                        "token": "olp_x", "name": "thesis"})
        assert done.status_code == 200
        assert "75% off" in done.text and "10 edits a month" in done.text      # the plans line on the success page


def test_without_an_invite_the_success_page_says_50_percent():
    from leafbridge.connect_link import mint_connect_code

    store, cipher, mcp = server()
    with client(mcp) as c:
        done = c.post("/connect", data={"code": mint_connect_code(cipher, "fr", "friend@example.com"),
                                        "overleaf_url": f"https://www.overleaf.com/project/{GOOD}",
                                        "token": "olp_x", "name": "thesis"})
    assert "50% off" in done.text and "75%" not in done.text


def test_a_pro_subscriber_sees_their_link_and_a_free_user_does_not():
    store, cipher, mcp = server()
    make(store, "fr", "friend@example.com", plan="pro", stripe_customer_id="cus_fr", referral_rewards=2, referral_pending=1)
    with client(mcp) as c:
        c.cookies.set("mila_session", mint_session(cipher, "fr", "friend@example.com"))
        page = c.get("/account").text
    code = run(store.get_user("fr")).referral_code
    assert f"https://milatexai.com/r/{code}" in page and "Invite a friend" in page
    assert "75% off their first month" in page and "50% off a month" in page
    assert "Rewards earned so far: 2 of 12" in page and "1 will come off your next invoice" in page

    store2, cipher2, mcp2 = server()
    make(store2, "fr", "friend@example.com")
    with client(mcp2) as c:
        c.cookies.set("mila_session", mint_session(cipher2, "fr", "friend@example.com"))
        free_page = c.get("/account").text
    assert "Invite a friend" not in free_page and run(store2.get_user("fr")).referral_code == ""


def test_the_account_page_quotes_the_friend_rate_to_an_invited_free_user():
    store, cipher, mcp = server()
    make(store, "fr", "friend@example.com", referred_by="inv")
    with client(mcp) as c:
        c.cookies.set("mila_session", mint_session(cipher, "fr", "friend@example.com"))
        page = c.get("/account").text
    assert "75% off their first month" in page and "$2.25" in page


def test_the_webhook_notes_the_first_payment_and_applies_waiting_credit_when_pro_starts():
    store, _ = world(inviter_plan="free", referral_rewards=1, referral_pending=1)
    f = FakeBilling()
    f.parse_event = lambda payload, sig: json.loads(payload)
    mcp = create_hosted_server(store=store, auth=False, billing=f, identity_provider=lambda: ("fr", "f@example.com"))
    paid = {"type": "invoice.paid", "created": 1700000000, "data": {"object": {
        "customer": "cus_fr", "billing_reason": "subscription_create", "amount_paid": 899}}}
    started = {"type": "checkout.session.completed", "data": {"object": {"client_reference_id": "inv", "customer": "cus_inv"}}}
    with TestClient(mcp.http_app()) as c:
        assert c.post("/stripe/webhook", content=json.dumps(paid), headers={"stripe-signature": "x"}).status_code == 200
        assert run(store.get_user("fr")).referral_paid_at == 1700000000.0
        assert f.credits == []                                              # inviter is still on the free plan
        assert c.post("/stripe/webhook", content=json.dumps(started), headers={"stripe-signature": "x"}).status_code == 200
    assert f.credits == [("cus_inv", 450, "referral-credit-inv-1")]
    assert run(store.get_user("inv")).referral_pending == 0


def call(mcp, tool):
    async def go():
        async with Client(mcp) as c:
            return (await c.call_tool(tool, {})).content[0].text
    return run(go())


def test_the_upgrade_tool_quotes_the_friend_rate_to_an_invited_user():
    class Stripe(FakeBilling):
        async def first_month_percent(self, customer_id, referred=False):
            return pricing.offer_percent(referred)

        async def create_checkout(self, user, customer_id=None):
            return "https://checkout.example/s", "cus_1"

    store = InMemoryStore()
    make(store, "fr", "friend@example.com", referred_by="inv")
    mcp = create_hosted_server(store=store, auth=False, billing=Stripe(), identity_provider=lambda: ("fr", "friend@example.com"))
    assert "75% off (about $2.25)" in call(mcp, "upgrade")


def test_pro_subscribers_are_told_about_the_invite_in_upgrade_and_manage_subscription():
    class Stripe(FakeBilling):
        async def create_portal(self, customer_id):
            return "https://portal.example/p"

    store = InMemoryStore()
    make(store, "fr", "friend@example.com", plan="pro", stripe_customer_id="cus_fr")
    mcp = create_hosted_server(store=store, auth=False, billing=Stripe(), base_url="https://milatexai.com",
                               identity_provider=lambda: ("fr", "friend@example.com"))
    code_note = lambda text: "https://milatexai.com/r/" in text and "75% off their first month" in text   # noqa: E731
    assert code_note(call(mcp, "upgrade")) and code_note(call(mcp, "manage_subscription"))


def test_first_steps_mention_the_plans_to_free_users_only():
    def first(**kw):
        store = InMemoryStore()
        cipher = TokenCipher(TokenCipher.generate_key())
        make(store, "fr", "friend@example.com", **kw)
        run(AccountService(store, cipher).connect_project(
            "fr", f"https://www.overleaf.com/project/{GOOD}", "olp_saved", "thesis"))
        mcp = create_hosted_server(store=store, cipher=cipher, auth=False,
                                   identity_provider=lambda: ("fr", "friend@example.com"))
        return call(mcp, "list_projects")

    plain = first()
    assert "first month is 50% off" in plain and "10 edits a month" in plain and "do not push it" in plain
    assert "first month is 75% off" in first(referred_by="inv")
    assert "first month is" not in first(plan="pro", stripe_customer_id="cus_1")           # Pro: no sales line
    assert "first month is" not in first(stripe_customer_id="cus_old")                      # past customer: no discount to quote
