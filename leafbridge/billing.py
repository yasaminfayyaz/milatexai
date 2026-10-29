"""Stripe billing for MiLatexAI Pro.

Onboarding mirrors the token-out-of-chat pattern: the authenticated user runs an
`upgrade` (or `manage_subscription`) tool and gets a secure Stripe-hosted link,
Checkout or the customer portal. Stripe then calls our ``/stripe/webhook``, which
flips the user between free and pro in the store. Card details only ever touch
Stripe's hosted pages, never us and never the chat.

Billing is optional: with no ``STRIPE_SECRET_KEY`` configured the tools report
that billing isn't set up, and the rest of the server runs unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from . import pricing
from .store import User

log = logging.getLogger("milatexai.billing")


class BillingError(Exception):
    pass


def _dig(obj, *path):
    """obj.a.b.c for a Stripe object or a plain dict, None if any step is missing."""
    for key in path:
        if obj is None:
            return None
        obj = obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
    return obj


class Billing:
    def __init__(
        self,
        *,
        api_key: str,
        price_id: str,
        webhook_secret: str,
        success_url: str,
        cancel_url: str,
        portal_return_url: str,
    ):
        self._api_key = api_key
        self.price_id = price_id
        self.webhook_secret = webhook_secret
        self.success_url = success_url
        self.cancel_url = cancel_url
        self.portal_return_url = portal_return_url
        self.enabled = bool(api_key and price_id)

    @classmethod
    def from_env(cls, base_url: str) -> "Billing":
        base = base_url.rstrip("/")
        return cls(
            api_key=os.environ.get("STRIPE_SECRET_KEY", ""),
            price_id=os.environ.get("STRIPE_PRICE_ID", ""),
            webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET", ""),
            success_url=f"{base}/account?status=success",
            cancel_url=f"{base}/account?status=cancelled",
            portal_return_url=f"{base}/account",
        )

    def _stripe(self):
        import stripe  # local import so the dep is only needed when billing is on

        stripe.api_key = self._api_key
        return stripe

    async def first_month_percent(self, customer_id: str | None, referred: bool = False) -> int:
        """The first-month discount this person gets: the offer's percentage if they have never
        had a subscription (the friend rate when they came through an invite link), otherwise 0.
        Any doubt (Stripe unreachable) means no discount."""
        percent = pricing.offer_percent(referred)
        if not self.enabled or not percent:
            return 0
        if not customer_id:
            return percent          # no Stripe customer yet, so never subscribed
        s = self._stripe()

        def _never_subscribed() -> bool:
            return not s.Subscription.list(customer=customer_id, status="all", limit=1).data

        try:
            return percent if await asyncio.to_thread(_never_subscribed) else 0
        except Exception as exc:  # noqa: BLE001
            log.warning("could not check past subscriptions (%s); no first-month discount", type(exc).__name__)
            return 0

    @staticmethod
    def _first_month_coupon(s, percent: int) -> str:
        """The id of the offer's Stripe coupon, creating it the first time it is needed."""
        coupon_id = pricing.first_month_coupon_id(percent)
        try:
            found = s.Coupon.retrieve(coupon_id)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "code", None) != "resource_missing":
                raise
            try:
                found = s.Coupon.create(id=coupon_id, percent_off=percent, duration="once",
                                        name=f"{percent}% off your first month")
            except Exception:  # noqa: BLE001  (another copy of the server made it a moment ago)
                found = s.Coupon.retrieve(coupon_id)
        if getattr(found, "valid", True) is False:
            raise BillingError("The first-month coupon is no longer valid.")
        return coupon_id

    async def create_checkout(self, user: User, customer_id: str | None = None) -> tuple[str, str]:
        """Create a subscription Checkout session. Returns (checkout_url, customer_id).

        A new subscriber gets the first-month discount applied automatically. If it cannot be
        applied for any reason the checkout still works at full price, with the promo-code box."""
        if not self.enabled:
            raise BillingError("Billing is not configured.")
        s = self._stripe()
        percent = await self.first_month_percent(customer_id, bool(user.referred_by))

        def _work() -> tuple[str, str]:
            cid = customer_id
            if not cid:
                cust = s.Customer.create(
                    email=user.email or None,
                    metadata={"user_id": user.user_id},
                )
                cid = cust.id
            params = dict(
                mode="subscription",
                customer=cid,
                line_items=[{"price": self.price_id, "quantity": 1}],
                client_reference_id=user.user_id,
                subscription_data={"metadata": {"user_id": user.user_id}},
                success_url=self.success_url,
                cancel_url=self.cancel_url,
            )
            discount = None
            if percent:
                try:
                    discount = [{"coupon": self._first_month_coupon(s, percent)}]
                except Exception as exc:  # noqa: BLE001
                    log.warning("first-month discount unavailable (%s); checkout continues without it",
                                type(exc).__name__)
            if discount:
                params["discounts"] = discount          # Stripe does not allow this together with the promo-code box
            else:
                params["allow_promotion_codes"] = True
            session = s.checkout.Session.create(**params)
            return session.url, cid

        return await asyncio.to_thread(_work)

    async def create_portal(self, customer_id: str) -> str:
        """Create a Stripe billing-portal session and return its URL."""
        if not self.enabled:
            raise BillingError("Billing is not configured.")
        s = self._stripe()

        def _work() -> str:
            sess = s.billing_portal.Session.create(
                customer=customer_id, return_url=self.portal_return_url
            )
            return sess.url

        return await asyncio.to_thread(_work)

    async def subscription_state(self, customer_id: str) -> dict:
        """Where this customer's subscription stands, straight from Stripe:
        {"state": "active" | "past_due" | "ended" | "unknown", "invoice_url", "since"}.

        "past_due" carries the unpaid invoice's payment page and when it was issued. Anything odd
        (Stripe unreachable, no subscription at all) is "unknown", which callers treat as no change."""
        s = self._stripe()

        def _work() -> dict:
            subs = s.Subscription.list(customer=customer_id, status="all", limit=20).data
            if not subs:
                return {"state": "unknown"}
            if any(x.status in ("active", "trialing") for x in subs):
                return {"state": "active"}
            late = [x for x in subs if x.status == "past_due"]
            if not late:
                return {"state": "ended"}
            sub = max(late, key=lambda x: x.created)
            inv_id = sub.latest_invoice if isinstance(sub.latest_invoice, str) else getattr(sub.latest_invoice, "id", None)
            url, since = None, None
            if inv_id:
                inv = s.Invoice.retrieve(inv_id)
                url = getattr(inv, "hosted_invoice_url", None)
                since = getattr(inv.status_transitions, "finalized_at", None) or inv.created
            return {"state": "past_due", "invoice_url": url, "since": since}

        try:
            return await asyncio.to_thread(_work)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read the subscription state (%s)", type(exc).__name__)
            return {"state": "unknown"}

    async def first_payment_check(self, customer_id: str) -> dict:
        """How this customer's first payment stands: {"state": "ok" | "refunded" | "unknown",
        "fingerprints": [card fingerprints used]}. Used before an inviter is rewarded."""
        s = self._stripe()

        def _work() -> dict:
            paid = [c for c in s.Charge.list(customer=customer_id, limit=20).data if getattr(c, "paid", False)]
            if not paid:
                return {"state": "unknown", "fingerprints": []}
            first = min(paid, key=lambda c: c.created)
            fp = _dig(first, "payment_method_details", "card", "fingerprint")
            bad = (getattr(first, "refunded", False) or (getattr(first, "amount_refunded", 0) or 0) > 0
                   or getattr(first, "disputed", False))
            return {"state": "refunded" if bad else "ok", "fingerprints": [fp] if fp else []}

        try:
            return await asyncio.to_thread(_work)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not check the first payment (%s)", type(exc).__name__)
            return {"state": "unknown", "fingerprints": []}

    async def card_fingerprints(self, customer_id: str) -> list[str]:
        """Fingerprints of the cards saved on a customer (empty when none or on any error)."""
        s = self._stripe()

        def _work() -> list[str]:
            out = []
            for pm in s.PaymentMethod.list(customer=customer_id, type="card", limit=20).data:
                fp = _dig(pm, "card", "fingerprint")
                if fp:
                    out.append(fp)
            return out

        try:
            return await asyncio.to_thread(_work)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not list saved cards (%s)", type(exc).__name__)
            return []

    async def unused_credit_cents(self, customer_id: str) -> int | None:
        """Credit still sitting on the customer's Stripe balance (0 when none). None if Stripe cannot say."""
        s = self._stripe()

        def _work() -> int:
            balance = getattr(s.Customer.retrieve(customer_id), "balance", 0) or 0
            return max(0, -int(balance))                  # Stripe stores a credit as a negative balance

        try:
            return await asyncio.to_thread(_work)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read the balance (%s)", type(exc).__name__)
            return None

    async def add_credit(self, customer_id: str, cents: int, key: str, note: str) -> bool:
        """Put ``cents`` of credit on the customer's Stripe balance; it comes off their next invoice.
        ``key`` makes a repeat of the same call a no-op. False if it could not be done."""
        s = self._stripe()

        def _work() -> None:
            s.Customer.create_balance_transaction(
                customer_id, amount=-int(cents), currency=pricing.PRO_BASE_CURRENCY,
                description=note, idempotency_key=key)

        try:
            await asyncio.to_thread(_work)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("could not add the credit (%s)", type(exc).__name__)
            return False

    def parse_event(self, payload: bytes, sig_header: str) -> dict:
        """Verify the webhook signature and return the event as a plain dict."""
        s = self._stripe()
        event = s.Webhook.construct_event(payload, sig_header, self.webhook_secret)
        # StripeObject.__str__ is JSON; converting sidesteps its attribute proxy.
        return json.loads(str(event))


# Subscription lifecycle -> plan. Kept as a pure function for easy testing.
# "past_due" is deliberately NOT here: a payment that failed keeps Pro on for a grace period (see
# dunning.py), and a late retry event must not undo the downgrade that follows if it stays unpaid.
_ACTIVE_STATUSES = {"active", "trialing"}
_ENDED_STATUSES = {"canceled", "unpaid", "incomplete_expired"}


def plan_change_from_event(event: dict) -> tuple[str | None, str | None, str | None]:
    """Map a Stripe webhook event to ``(user_id, new_plan, customer_id)``.

    ``new_plan`` is None when the event carries no plan change to apply.
    """
    etype = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}

    if etype == "checkout.session.completed":
        return obj.get("client_reference_id"), "pro", obj.get("customer")

    if etype in ("customer.subscription.updated", "customer.subscription.deleted"):
        user_id = (obj.get("metadata") or {}).get("user_id")
        customer_id = obj.get("customer")
        status = obj.get("status")
        if etype == "customer.subscription.deleted" or status in _ENDED_STATUSES:
            return user_id, "free", customer_id
        if status in _ACTIVE_STATUSES:
            return user_id, "pro", customer_id
        return user_id, None, customer_id

    return None, None, None


def dunning_change_from_event(event: dict) -> dict | None:
    """Map a Stripe webhook event to a payment-problem change, or None.

    {"action": "start", "customer", "invoice_url", "at"}  a renewal payment did not go through
    {"action": "clear", "customer"}                        the customer is paid up (or is gone)
    """
    etype = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}
    customer = obj.get("customer")
    if not isinstance(customer, str) or not customer:
        return None
    if etype in ("invoice.payment_failed", "invoice.payment_action_required"):
        parent = ((obj.get("parent") or {}).get("subscription_details") or {})
        if not (obj.get("subscription") or parent.get("subscription")):
            return None                                     # not a subscription invoice
        if obj.get("billing_reason") == "subscription_create":
            return None                                     # the very first payment: they never became Pro
        return {"action": "start", "customer": customer, "invoice_url": obj.get("hosted_invoice_url") or None,
                "at": float(event.get("created") or 0) or None}
    if etype in ("invoice.paid", "invoice.payment_succeeded", "customer.subscription.deleted"):
        return {"action": "clear", "customer": customer}
    if etype == "customer.subscription.updated":
        status = obj.get("status")
        if status in _ACTIVE_STATUSES:
            return {"action": "clear", "customer": customer}
        if status == "past_due":
            return {"action": "start", "customer": customer, "invoice_url": None, "at": float(event.get("created") or 0) or None}
    return None


def first_payment_from_event(event: dict) -> dict | None:
    """A subscription's first real payment went through: {"customer", "at"}, else None.
    Zero-amount invoices (a fully discounted start) do not count."""
    if event.get("type") not in ("invoice.paid", "invoice.payment_succeeded"):
        return None
    obj = (event.get("data") or {}).get("object") or {}
    customer = obj.get("customer")
    if not isinstance(customer, str) or not customer:
        return None
    if obj.get("billing_reason") != "subscription_create" or not (obj.get("amount_paid") or 0) > 0:
        return None
    return {"customer": customer, "at": float(event.get("created") or 0) or None}
