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

    async def first_month_percent(self, customer_id: str | None) -> int:
        """The first-month discount this person gets: the offer's percentage if they have never
        had a subscription, otherwise 0. Any doubt (Stripe unreachable) means no discount."""
        percent = pricing.FIRST_MONTH_PERCENT
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
        percent = await self.first_month_percent(customer_id)

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

    def parse_event(self, payload: bytes, sig_header: str) -> dict:
        """Verify the webhook signature and return the event as a plain dict."""
        s = self._stripe()
        event = s.Webhook.construct_event(payload, sig_header, self.webhook_secret)
        # StripeObject.__str__ is JSON; converting sidesteps its attribute proxy.
        return json.loads(str(event))


# Subscription lifecycle -> plan. Kept as a pure function for easy testing.
_ACTIVE_STATUSES = {"active", "trialing", "past_due"}
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
