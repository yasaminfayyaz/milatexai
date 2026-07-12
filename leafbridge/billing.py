"""Stripe billing for MiLatexAI Pro.

Onboarding mirrors the token-out-of-chat pattern: the authenticated user runs an
`upgrade` (or `manage_subscription`) tool and gets a secure Stripe-hosted link —
Checkout or the customer portal. Stripe then calls our ``/stripe/webhook``, which
flips the user between free and pro in the store. Card details only ever touch
Stripe's hosted pages, never us and never the chat.

Billing is optional: with no ``STRIPE_SECRET_KEY`` configured the tools report
that billing isn't set up, and the rest of the server runs unchanged.
"""

from __future__ import annotations

import asyncio
import json
import os

from .store import User


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

    async def create_checkout(self, user: User, customer_id: str | None = None) -> tuple[str, str]:
        """Create a subscription Checkout session. Returns (checkout_url, customer_id)."""
        if not self.enabled:
            raise BillingError("Billing is not configured.")
        s = self._stripe()

        def _work() -> tuple[str, str]:
            cid = customer_id
            if not cid:
                cust = s.Customer.create(
                    email=user.email or None,
                    metadata={"user_id": user.user_id},
                )
                cid = cust.id
            session = s.checkout.Session.create(
                mode="subscription",
                customer=cid,
                line_items=[{"price": self.price_id, "quantity": 1}],
                client_reference_id=user.user_id,
                subscription_data={"metadata": {"user_id": user.user_id}},
                success_url=self.success_url,
                cancel_url=self.cancel_url,
                allow_promotion_codes=True,
            )
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
