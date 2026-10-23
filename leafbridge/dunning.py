"""What happens when a Pro renewal payment does not go through.

The customer keeps Pro for a grace period, gets two emails (one when the payment fails, one a couple
of days before Pro ends), and moves to the free plan if it is still unpaid at the end. Paying at any
point, through the link in the emails or Stripe's own retry, brings everything back to normal.

Three things feed this, all idempotent:
  * Stripe webhooks (hosted.py) start or clear the countdown the moment something happens,
  * a sweep every few hours, claimed by exactly one running copy of the server, reconciles with
    Stripe (so a missed webhook is caught), sends reminders that are due and ends Pro when time is up (only after both emails have gone out),
  * the emails themselves go out through mailer.py.

The downgrade never happens before at least the first email has gone out, and Stripe's own state
is checked again right before it, so a customer who just paid is never downgraded.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from . import pricing

log = logging.getLogger("milatexai.dunning")

GRACE_DAYS = 7                 # Pro stays on this long after the first failed payment
REMIND_DAYS_BEFORE_END = 2     # the second email goes out this long before Pro ends
DAY = 86400
SWEEP_EVERY = 30 * 60          # how often each copy looks whether a sweep is due
SWEEP_WINDOW_HOURS = 6         # at most one sweep per window, across all copies
FIRST_SWEEP_DELAY = 10 * 60    # let a new copy warm up first

MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December")


def grace_end(since: float) -> float:
    return since + GRACE_DAYS * DAY


def month_day(ts: float) -> str:
    """"October 26" (UTC, English month names whatever the server's locale)."""
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    return f"{MONTHS[d.month - 1]} {d.day}"


def next_step(user, now: float) -> str | None:
    """What is due for this account right now: "first", "reminder", "end" or None."""
    if user.is_admin or user.plan != "pro" or not user.past_due_since:
        return None
    end = grace_end(user.past_due_since)
    if user.dunning_stage >= 2 and now >= end:
        return "end"            # never before the customer has had both emails
    if user.dunning_stage < 2 and now >= end - REMIND_DAYS_BEFORE_END * DAY:
        return "reminder"
    if user.dunning_stage < 1:
        return "first"
    return None


KIND_FOR_STEP = {"first": "payment_failed", "reminder": "payment_reminder"}
STAGE_AFTER = {"first": 1, "reminder": 2}


async def _send_step(store, mailer, user, step: str, pay_url: str | None, now: float) -> bool:
    """Send the email for ``step`` once and record it. False if it could not go out."""
    if not mailer.enabled:
        log.warning("customer emails are not configured; not sending the %s notice", step)
        return False
    if not user.email or not pay_url:
        log.warning("no address or payment link for a %s notice; will try again", step)
        return False
    # One send per account, step and hour, even if several copies of the server race to do it.
    if not await store.claim_job("dunning-mail", f"{user.user_id}:{step}:{int(now // 3600)}"):
        return False
    ok = await mailer.send(KIND_FOR_STEP[step], user.email, pay_url, month_day(grace_end(user.past_due_since)),
                           pricing.display_price())
    if ok:
        user.dunning_stage = STAGE_AFTER[step]
        await store.upsert_user(user)
    return ok


async def on_payment_problem(store, mailer, customer_id: str, pay_url: str | None, at: float | None, now: float) -> str:
    """A webhook says a renewal payment failed. Start the countdown and send the first email."""
    user = await store.find_user_by_customer(customer_id)
    if user is None or user.is_admin or user.plan != "pro":
        return "ignored"
    if not user.past_due_since:
        user.past_due_since = float(at or now)
        await store.upsert_user(user)
    step = next_step(user, now)
    if step in ("first", "reminder") and pay_url:
        await _send_step(store, mailer, user, step, pay_url, now)
    return "started"


async def on_paid_up(store, customer_id: str) -> str:
    user = await store.find_user_by_customer(customer_id)
    if user is None or not (user.past_due_since or user.dunning_stage):
        return "nothing"
    user.past_due_since, user.dunning_stage = 0.0, 0
    await store.upsert_user(user)
    return "cleared"


async def sweep(store, billing, mailer, now: float | None = None) -> dict:
    """Reconcile every Pro account that has a Stripe customer with Stripe, then do what is due."""
    now = time.time() if now is None else now
    report = {"checked": 0, "started": 0, "cleared": 0, "first": 0, "reminder": 0, "ended": 0, "unknown": 0, "held": 0}
    for listed in await store.list_users():
        if listed.is_admin or listed.plan != "pro" or not listed.stripe_customer_id:
            continue
        report["checked"] += 1
        user = await store.get_user(listed.user_id)       # a fresh copy: never write back a stale one
        if user is None or user.plan != "pro":
            continue
        state = await billing.subscription_state(user.stripe_customer_id)
        kind = state.get("state")
        if kind == "unknown":
            report["unknown"] += 1
        elif kind == "active":
            if user.past_due_since or user.dunning_stage:
                user.past_due_since, user.dunning_stage = 0.0, 0
                await store.upsert_user(user)
                report["cleared"] += 1
        elif kind == "ended":
            # Stripe has nothing active or late for this customer: the subscription is over.
            user.plan, user.past_due_since, user.dunning_stage = "free", 0.0, 0
            await store.upsert_user(user)
            report["ended"] += 1
        elif kind == "past_due":
            if not user.past_due_since:
                user.past_due_since = float(state.get("since") or now)
                await store.upsert_user(user)
                report["started"] += 1
            step = next_step(user, now)
            if step in ("first", "reminder"):
                if await _send_step(store, mailer, user, step, state.get("invoice_url"), now):
                    report[step] += 1
                else:
                    report["held"] += 1
            elif step == "end":
                user.plan, user.past_due_since, user.dunning_stage = "free", 0.0, 0
                await store.upsert_user(user)
                report["ended"] += 1
    return report


class DunningLoop:
    """Runs the sweep in the background of each copy; the claim makes sure only one copy does the work."""

    def __init__(self, store, billing, mailer, *, every: float = SWEEP_EVERY, first_delay: float = FIRST_SWEEP_DELAY,
                 clock=time.time) -> None:
        self.store, self.billing, self.mailer = store, billing, mailer
        self.every, self.first_delay, self._clock = every, first_delay, clock
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _run(self) -> None:
        await asyncio.sleep(self.first_delay)
        while True:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001
                log.warning("the payment sweep failed (%s)", type(exc).__name__)
            await asyncio.sleep(self.every)

    async def tick(self) -> dict | None:
        """One sweep per six-hour window, across all running copies."""
        now = self._clock()
        d = datetime.fromtimestamp(now, tz=timezone.utc)
        window = f"{d:%Y-%m-%d}-{d.hour // SWEEP_WINDOW_HOURS}"
        if not self.billing.enabled or not await self.store.claim_job("dunning-sweep", window):
            return None
        report = await sweep(self.store, self.billing, self.mailer, now)
        if any(report[k] for k in ("started", "cleared", "first", "reminder", "ended", "held")):
            log.info("payment sweep: %s", report)
        return report
