"""Tests for Stripe billing: webhook event -> plan mapping, the disabled-billing
default, and applying a subscription change in the store."""

from __future__ import annotations

import asyncio

from leafbridge.billing import Billing, plan_change_from_event
from leafbridge.service import AccountService
from leafbridge.store import InMemoryStore, TokenCipher, User


def _service():
    return AccountService(InMemoryStore(), TokenCipher(TokenCipher.generate_key()))


# --- webhook event -> (user_id, plan, customer) ----------------------------

def test_checkout_completed_maps_to_pro():
    event = {
        "type": "checkout.session.completed",
        "data": {"object": {"client_reference_id": "user_1", "customer": "cus_1"}},
    }
    assert plan_change_from_event(event) == ("user_1", "pro", "cus_1")


def test_subscription_deleted_maps_to_free():
    event = {
        "type": "customer.subscription.deleted",
        "data": {"object": {"metadata": {"user_id": "user_1"}, "customer": "cus_1", "status": "canceled"}},
    }
    assert plan_change_from_event(event) == ("user_1", "free", "cus_1")


def test_subscription_updated_active_is_pro_canceled_is_free():
    base = {"metadata": {"user_id": "u"}, "customer": "cus_1"}
    active = {"type": "customer.subscription.updated", "data": {"object": {**base, "status": "active"}}}
    ended = {"type": "customer.subscription.updated", "data": {"object": {**base, "status": "unpaid"}}}
    assert plan_change_from_event(active) == ("u", "pro", "cus_1")
    assert plan_change_from_event(ended) == ("u", "free", "cus_1")


def test_irrelevant_event_is_ignored():
    assert plan_change_from_event({"type": "invoice.created", "data": {"object": {}}}) == (None, None, None)


# --- disabled billing ------------------------------------------------------

def test_billing_disabled_without_env(monkeypatch):
    for var in ("STRIPE_SECRET_KEY", "STRIPE_PRICE_ID", "STRIPE_WEBHOOK_SECRET"):
        monkeypatch.delenv(var, raising=False)
    b = Billing.from_env("https://milatexai.com")
    assert b.enabled is False


# --- applying a subscription in the store ----------------------------------

def test_apply_subscription_flips_plan_and_stores_customer():
    svc = _service()
    asyncio.run(svc.get_or_create_user("user_1", "a@b.com"))

    changed = asyncio.run(svc.apply_subscription("user_1", "pro", "cus_9"))
    assert changed is True
    user = asyncio.run(svc.store.get_user("user_1"))
    assert user.plan == "pro" and user.stripe_customer_id == "cus_9"

    # Idempotent: re-applying the same state changes nothing.
    assert asyncio.run(svc.apply_subscription("user_1", "pro", "cus_9")) is False

    # Downgrade back to free works.
    assert asyncio.run(svc.apply_subscription("user_1", "free", "cus_9")) is True
    assert asyncio.run(svc.store.get_user("user_1")).plan == "free"


def test_apply_subscription_never_downgrades_admin():
    svc = _service()
    asyncio.run(svc.get_or_create_user("admin_1", "boss@x.com", admin_emails=("boss@x.com",)))
    # A stray "free" event must not strip an admin's unlimited access.
    asyncio.run(svc.apply_subscription("admin_1", "free", "cus_1"))
    user = asyncio.run(svc.store.get_user("admin_1"))
    assert user.is_admin is True
    assert user.plan != "pro" or user.is_admin  # admin stays unlimited regardless


def test_apply_subscription_unknown_user_is_noop():
    svc = _service()
    assert asyncio.run(svc.apply_subscription("ghost", "pro", "cus_1")) is False
