"""When a Pro renewal payment does not go through: grace period, two emails, then the free plan.

The rules that matter: nobody loses Pro before they have been told, nobody who has paid is
downgraded, a missed webhook is caught by the sweep, and every email goes out at most once."""

from __future__ import annotations

import asyncio
import json
import os
import warnings

from starlette.testclient import TestClient

from leafbridge import dunning, site
from leafbridge.billing import dunning_change_from_event, plan_change_from_event
from leafbridge.dunning import DAY, DunningLoop, next_step, sweep
from leafbridge.store import InMemoryStore, User

warnings.filterwarnings("ignore", category=DeprecationWarning)
os.environ.setdefault("WORKOS_AUTHKIT_DOMAIN", "https://placeholder.authkit.invalid")

T0 = 1_760_000_000.0          # the moment the payment failed
LINK = "https://invoice.stripe.com/i/acct_x/live_abc?s=ap"


class FakeBilling:
    enabled = True

    def __init__(self, **states):
        self.states = states          # customer id -> {"state": ..., ...}
        self.calls = []

    async def subscription_state(self, customer_id):
        self.calls.append(customer_id)
        return self.states.get(customer_id, {"state": "unknown"})


class FakeMailer:
    def __init__(self, enabled=True, works=True):
        self.enabled, self.works, self.sent = enabled, works, []

    async def send(self, kind, to, pay_url, ends_on, amount):
        if not self.works:
            return False
        self.sent.append((kind, to, pay_url, ends_on, amount))
        return True


def pro_user(store, uid="u1", customer="cus_1", email="rishabh@example.com", **kw):
    u = User(user_id=uid, email=email, plan="pro", stripe_customer_id=customer, **kw)
    asyncio.run(store.upsert_user(u))
    return u


def load(store, uid="u1"):
    return asyncio.run(store.get_user(uid))


def late(since=T0):
    return {"state": "past_due", "invoice_url": LINK, "since": since}


def run(coro):
    return asyncio.run(coro)


# --- the timeline ---------------------------------------------------------------------------

def test_the_timeline_of_a_failed_payment():
    u = User(user_id="u", email="a@b.c", plan="pro", past_due_since=T0)
    assert next_step(u, T0) == "first"
    u.dunning_stage = 1
    assert next_step(u, T0 + 3 * DAY) is None                      # quiet in the middle
    assert next_step(u, T0 + 5 * DAY) == "reminder"                # two days before it ends
    u.dunning_stage = 2
    assert next_step(u, T0 + 6.5 * DAY) is None
    assert next_step(u, T0 + 7 * DAY) == "end"
    assert dunning.month_day(dunning.grace_end(T0)) == "October 16"       # T0 is Oct 9, 2025 UTC, plus 7 days


def test_nobody_is_downgraded_before_both_emails_have_gone_out():
    u = User(user_id="u", email="a@b.c", plan="pro", past_due_since=T0, dunning_stage=0)
    assert next_step(u, T0 + 8 * DAY) == "reminder"                # still owes a notice, so no "end" yet
    assert next_step(u, T0 + 5.5 * DAY) == "reminder"              # a late first notice becomes the reminder
    u.dunning_stage = 1
    assert next_step(u, T0 + 8 * DAY) == "reminder"                # a long outage of the sweep cannot skip the reminder
    u.dunning_stage = 2
    assert next_step(u, T0 + 8 * DAY) == "end"


def test_only_paying_non_admin_accounts_are_ever_touched():
    assert next_step(User(user_id="u", email="a", plan="free", past_due_since=T0), T0 + 9 * DAY) is None
    assert next_step(User(user_id="u", email="a", plan="pro", is_admin=True, past_due_since=T0), T0 + 9 * DAY) is None
    assert next_step(User(user_id="u", email="a", plan="pro"), T0 + 9 * DAY) is None      # paid up


# --- webhooks -------------------------------------------------------------------------------

def test_a_failed_payment_event_starts_the_countdown_and_emails_once():
    store, mailer = InMemoryStore(), FakeMailer()
    pro_user(store)
    for _ in range(2):          # Stripe sends "payment failed" and "action required" together
        assert run(dunning.on_payment_problem(store, mailer, "cus_1", LINK, T0, T0 + 5)) == "started"
    u = load(store)
    assert u.past_due_since == T0 and u.dunning_stage == 1 and u.plan == "pro"
    assert mailer.sent == [("payment_failed", "rishabh@example.com", LINK, "October 16", "$8.99")]


def test_events_for_other_people_are_ignored():
    store, mailer = InMemoryStore(), FakeMailer()
    pro_user(store, uid="adm", customer="cus_a").is_admin
    asyncio.run(store.upsert_user(User(user_id="adm", email="a", plan="pro", is_admin=True, stripe_customer_id="cus_a")))
    asyncio.run(store.upsert_user(User(user_id="fr", email="f", plan="free", stripe_customer_id="cus_f")))
    for cust in ("cus_a", "cus_f", "cus_nobody"):
        assert run(dunning.on_payment_problem(store, mailer, cust, LINK, T0, T0)) == "ignored"
    assert mailer.sent == [] and load(store, "adm").past_due_since == 0 and load(store, "fr").past_due_since == 0


def test_paying_clears_everything():
    store, mailer = InMemoryStore(), FakeMailer()
    pro_user(store, past_due_since=T0, dunning_stage=2)
    assert run(dunning.on_paid_up(store, "cus_1")) == "cleared"
    u = load(store)
    assert (u.past_due_since, u.dunning_stage, u.plan) == (0.0, 0, "pro")
    assert run(dunning.on_paid_up(store, "cus_1")) == "nothing"


def test_stripe_events_map_to_the_right_changes():
    inv = {"customer": "cus_1", "subscription": "sub_1", "hosted_invoice_url": LINK, "billing_reason": "subscription_cycle"}
    failed = {"type": "invoice.payment_failed", "created": 1760000000, "data": {"object": inv}}
    assert dunning_change_from_event(failed) == {"action": "start", "customer": "cus_1", "invoice_url": LINK, "at": 1760000000.0}
    new_api = {"type": "invoice.payment_action_required", "created": 5,
               "data": {"object": {"customer": "cus_1", "parent": {"subscription_details": {"subscription": "sub_1"}}, "hosted_invoice_url": LINK}}}
    assert dunning_change_from_event(new_api)["action"] == "start"
    first_payment = {"type": "invoice.payment_failed", "data": {"object": {**inv, "billing_reason": "subscription_create"}}}
    assert dunning_change_from_event(first_payment) is None                # they never became Pro
    one_off = {"type": "invoice.payment_failed", "data": {"object": {"customer": "cus_1", "hosted_invoice_url": LINK}}}
    assert dunning_change_from_event(one_off) is None                       # not a subscription invoice
    for t in ("invoice.paid", "invoice.payment_succeeded", "customer.subscription.deleted"):
        assert dunning_change_from_event({"type": t, "data": {"object": {"customer": "cus_1"}}}) == {"action": "clear", "customer": "cus_1"}
    sub = lambda status: {"type": "customer.subscription.updated", "created": 9, "data": {"object": {"customer": "cus_1", "status": status}}}   # noqa: E731
    assert dunning_change_from_event(sub("active"))["action"] == "clear"
    assert dunning_change_from_event(sub("past_due"))["action"] == "start"
    assert dunning_change_from_event(sub("canceled")) is None
    assert dunning_change_from_event({"type": "invoice.created", "data": {"object": {"customer": "cus_1"}}}) is None
    assert dunning_change_from_event({"type": "invoice.paid", "data": {"object": {}}}) is None


def test_a_late_retry_event_cannot_undo_a_downgrade():
    base = {"metadata": {"user_id": "u"}, "customer": "cus_1"}
    past_due = {"type": "customer.subscription.updated", "data": {"object": {**base, "status": "past_due"}}}
    assert plan_change_from_event(past_due) == ("u", None, "cus_1")        # no plan change either way


# --- the sweep -------------------------------------------------------------------------------

def test_the_sweep_catches_an_unpaid_renewal_nobody_told_us_about():
    store, mailer, billing = InMemoryStore(), FakeMailer(), FakeBilling(cus_1=late())
    pro_user(store)
    rep = run(sweep(store, billing, mailer, T0 + 3 * 3600))
    u = load(store)
    assert rep["started"] == 1 and rep["first"] == 1
    assert u.past_due_since == T0 and u.dunning_stage == 1 and u.plan == "pro"
    assert [m[0] for m in mailer.sent] == ["payment_failed"]
    run(sweep(store, billing, mailer, T0 + 9 * 3600))                       # same day: nothing more
    assert len(mailer.sent) == 1


def test_two_emails_then_the_free_plan():
    store, mailer, billing = InMemoryStore(), FakeMailer(), FakeBilling(cus_1=late())
    pro_user(store)
    for day in (0, 1, 2, 3, 4):
        run(sweep(store, billing, mailer, T0 + day * DAY + 3600))
    assert [m[0] for m in mailer.sent] == ["payment_failed"] and load(store).plan == "pro"
    run(sweep(store, billing, mailer, T0 + 5 * DAY + 3600))                 # two days before it ends
    assert [m[0] for m in mailer.sent] == ["payment_failed", "payment_reminder"]
    assert mailer.sent[1][3] == "October 16"
    for day in (5, 6):
        run(sweep(store, billing, mailer, T0 + day * DAY + 7200))
    assert load(store).plan == "pro" and len(mailer.sent) == 2
    rep = run(sweep(store, billing, mailer, T0 + 7 * DAY + 60))
    u = load(store)
    assert rep["ended"] == 1 and (u.plan, u.past_due_since, u.dunning_stage) == ("free", 0.0, 0)
    assert u.stripe_customer_id == "cus_1" and len(mailer.sent) == 2        # exactly two emails before the end


def test_paying_before_the_end_keeps_pro():
    store, mailer, billing = InMemoryStore(), FakeMailer(), FakeBilling(cus_1=late())
    pro_user(store)
    run(sweep(store, billing, mailer, T0 + 3600))
    billing.states["cus_1"] = {"state": "active"}
    rep = run(sweep(store, billing, mailer, T0 + 7 * DAY + 60))
    u = load(store)
    assert rep["cleared"] == 1 and rep["ended"] == 0 and u.plan == "pro" and not u.past_due_since


def test_if_emails_cannot_be_sent_nobody_is_downgraded():
    store, billing = InMemoryStore(), FakeBilling(cus_1=late())
    pro_user(store)
    for mailer in (FakeMailer(enabled=False), FakeMailer(works=False)):
        rep = run(sweep(store, billing, mailer, T0 + 10 * DAY))
        u = load(store)
        assert u.plan == "pro" and u.dunning_stage == 0 and rep["held"] == 1 and rep["ended"] == 0


def test_stripe_trouble_or_missing_data_changes_nothing():
    store, mailer = InMemoryStore(), FakeMailer()
    pro_user(store, past_due_since=T0, dunning_stage=2)
    rep = run(sweep(store, FakeBilling(), mailer, T0 + 20 * DAY))           # Stripe unreachable: "unknown"
    assert rep["unknown"] == 1 and load(store).plan == "pro"
    no_link = FakeBilling(cus_1={"state": "past_due", "invoice_url": None, "since": T0})
    pro_user(store, uid="u2", customer="cus_1", email="x@example.com")
    run(sweep(store, no_link, mailer, T0 + 100))
    assert mailer.sent == []                                                # no payment link, nothing to send yet


def test_a_cancelled_subscription_is_caught_even_if_the_webhook_was_missed():
    store, mailer = InMemoryStore(), FakeMailer()
    pro_user(store)
    run(sweep(store, FakeBilling(cus_1={"state": "ended"}), mailer, T0))
    assert load(store).plan == "free" and mailer.sent == []


def test_free_admin_and_complimentary_accounts_are_never_touched():
    store, mailer, billing = InMemoryStore(), FakeMailer(), FakeBilling(cus_f=late(), cus_a=late())
    asyncio.run(store.upsert_user(User(user_id="f", email="f@x", plan="free", stripe_customer_id="cus_f")))
    asyncio.run(store.upsert_user(User(user_id="a", email="a@x", plan="pro", is_admin=True, stripe_customer_id="cus_a")))
    asyncio.run(store.upsert_user(User(user_id="rev", email="reviewer@milatexai.com", plan="pro")))     # Pro without paying
    rep = run(sweep(store, billing, mailer, T0 + 30 * DAY))
    assert rep["checked"] == 0 and billing.calls == [] and mailer.sent == []
    assert load(store, "rev").plan == "pro" and load(store, "a").plan == "pro"


def test_the_sweep_runs_once_per_window_even_with_several_copies():
    store, billing, mailer = InMemoryStore(), FakeBilling(cus_1=late()), FakeMailer()
    pro_user(store)
    now = [T0 + 3600]
    copies = [DunningLoop(store, billing, mailer, clock=lambda: now[0]) for _ in range(3)]

    async def go():
        return [await c.tick() for c in copies]
    reports = run(go())
    assert sum(1 for r in reports if r is not None) == 1                    # one copy did the work
    now[0] += 7 * 3600                                                      # the next window
    assert run(copies[1].tick()) is not None


def test_a_disabled_billing_setup_never_sweeps():
    store, billing = InMemoryStore(), FakeBilling()
    billing.enabled = False
    assert run(DunningLoop(store, billing, FakeMailer()).tick()) is None


# --- the web side ------------------------------------------------------------------------------

def test_the_account_page_warns_a_pro_user_whose_payment_failed():
    acct = {"email": "a@b.c", "plan": "pro", "has_customer": True, "past_due_until": "October 26"}
    page = site.render_account(signed_in=True, account=acct)
    assert "didn't go through" in page and "October 26" in page
    assert "didn't go through" not in site.render_account(signed_in=True, account={**acct, "past_due_until": ""})
    assert "didn't go through" not in site.render_account(signed_in=True, account={**acct, "plan": "free"})


def test_the_stripe_webhook_route_starts_the_countdown_and_sends_the_first_email():
    from leafbridge.hosted import create_hosted_server

    store, mailer = InMemoryStore(), FakeMailer()
    pro_user(store)
    event = {"type": "invoice.payment_failed", "created": int(__import__("time").time()), "data": {"object": {
        "customer": "cus_1", "subscription": "sub_1", "hosted_invoice_url": LINK, "billing_reason": "subscription_cycle"}}}

    class StripeStub:
        enabled = True

        def parse_event(self, payload, sig):
            return json.loads(payload)

    mcp = create_hosted_server(store=store, auth=False, billing=StripeStub(), mailer=mailer,
                               identity_provider=lambda: ("u1", "rishabh@example.com"))
    with TestClient(mcp.http_app()) as client:
        r = client.post("/stripe/webhook", content=json.dumps(event), headers={"stripe-signature": "x"})
        assert r.status_code == 200
        paid = {"type": "invoice.paid", "data": {"object": {"customer": "cus_1"}}}
        again = client.post("/stripe/webhook", content=json.dumps(paid), headers={"stripe-signature": "x"})
        assert again.status_code == 200
    assert [m[0] for m in mailer.sent] == ["payment_failed"]
    u = load(store)
    assert u.plan == "pro" and u.past_due_since == 0 and u.dunning_stage == 0             # paid up again


def test_the_signature_logo_is_served_for_emails():
    from leafbridge.hosted import create_hosted_server

    mcp = create_hosted_server(store=InMemoryStore(), auth=False, identity_provider=lambda: ("u1", "a@example.com"))
    with TestClient(mcp.http_app()) as client:
        r = client.get("/email-signature-logo.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:8] == b"\x89PNG\r\n\x1a\n"
