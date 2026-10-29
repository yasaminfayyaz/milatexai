"""Invite a friend.

A Pro subscriber shares a personal link. A friend who arrives through it, and later subscribes for
the first time, gets a bigger first-month discount than the standard one (pricing.REFERRAL_FRIEND_PERCENT,
applied at checkout by billing.py). When that friend's first payment has stood for a few days without
being refunded, the inviter gets a reward: the reward percentage off one month, as credit on their
Stripe balance, up to a cap.

Everything here is idempotent. The pieces:
  * ``/r/<code>`` (hosted.py) remembers the code in a cookie,
  * ``attach`` links the friend to the inviter the first time the friend is seen with that cookie,
  * ``on_first_payment`` (Stripe webhook) notes when the friend's first real payment went through,
  * ``sweep`` (every few hours, with the payment sweep) settles rewards once the hold has passed and
    applies credit that was waiting.
"""

from __future__ import annotations

import logging
import re
import secrets
import time

from . import pricing

log = logging.getLogger("milatexai.referrals")

CODE_RE = re.compile(r"^[0-9a-f]{8}$")
DAY = 86400
_CACHE_SECONDS = 600
_cache: dict[str, tuple[str | None, float]] = {}      # code -> (user_id or None, expires): keeps /r/<code> cheap


def link(base_url: str, code: str) -> str:
    return f"{base_url.rstrip('/')}/r/{code}"


def normalize_email(email: str) -> str:
    """One mailbox, one spelling: lower case, no +tag, and for Gmail no dots. Used to refuse inviting yourself."""
    local, _, domain = (email or "").strip().lower().partition("@")
    local = local.split("+", 1)[0]
    if domain in ("gmail.com", "googlemail.com"):
        local, domain = local.replace(".", ""), "gmail.com"
    return f"{local}@{domain}"


async def ensure_code(store, user) -> str:
    """This person's own invite code, made the first time it is asked for."""
    if user.referral_code:
        return user.referral_code
    taken = {u.referral_code for u in await store.list_users() if u.referral_code}
    code = secrets.token_hex(4)
    while code in taken:
        code = secrets.token_hex(4)
    user.referral_code = code
    await store.upsert_user(user)
    _cache.pop(code, None)
    return code


async def find_by_code(store, code: str, now: float | None = None):
    """The account that owns this code, or None. Anything that is not a well-formed code costs nothing."""
    code = (code or "").strip().lower()
    if not CODE_RE.match(code):
        return None
    now = time.time() if now is None else now
    hit = _cache.get(code)
    if hit and hit[1] > now:
        return await store.get_user(hit[0]) if hit[0] else None
    owner = next((u for u in await store.list_users() if u.referral_code == code), None)
    _cache[code] = (owner.user_id if owner else None, now + (_CACHE_SECONDS if owner else 60))
    return owner


async def attach(store, user, code: str) -> bool:
    """Link ``user`` to the inviter behind ``code``. Only once, never to themselves, and only for an
    ordinary account. True if a link was made."""
    if not pricing.referral_enabled() or user.referred_by or user.is_admin:
        return False
    inviter = await find_by_code(store, code)
    if inviter is None or inviter.user_id == user.user_id:
        return False
    if user.email and inviter.email and normalize_email(user.email) == normalize_email(inviter.email):
        return False
    user.referred_by = inviter.user_id
    await store.upsert_user(user)
    return True


async def on_first_payment(store, customer_id: str, at: float | None, now: float | None = None) -> str:
    """The webhook says this customer's first real payment went through. Start the hold."""
    friend = await store.find_user_by_customer(customer_id)
    if friend is None or not friend.referred_by or friend.referral_paid_at or friend.referral_done:
        return "ignored"
    friend.referral_paid_at = float(at or (time.time() if now is None else now))
    await store.upsert_user(friend)
    return "noted"


async def apply_pending(store, billing, user_id: str) -> int:
    """Move rewards that are waiting onto the inviter's Stripe balance. Only a Pro subscriber with a
    Stripe customer is credited; for anyone else the reward waits. Returns how many were applied."""
    applied = 0
    while True:
        user = await store.get_user(user_id)       # fresh each round: never write back a stale copy
        if (user is None or user.referral_pending <= 0 or not user.stripe_customer_id
                or user.is_admin or user.plan != "pro"):
            return applied
        number = user.referral_rewards - user.referral_pending + 1     # which reward this is, 1 to the cap
        ok = await billing.add_credit(
            user.stripe_customer_id, pricing.referral_credit_cents(), f"referral-credit-{user.user_id}-{number}",
            f"Invite a friend: {pricing.REFERRAL_REWARD_PERCENT}% off one month")
        if not ok:
            return applied
        user.referral_pending -= 1
        await store.upsert_user(user)
        applied += 1


async def _settle(store, billing, friend, now: float) -> str:
    """Decide the inviter's reward for one friend whose hold has passed. Returns what happened:
    "rewarded", "skipped" (decided no), or "waiting" (could not tell; try again later)."""
    check = await billing.first_payment_check(friend.stripe_customer_id)
    if check.get("state") == "unknown":
        return "waiting"
    inviter = await store.get_user(friend.referred_by)
    no_reward = (check.get("state") != "ok" or inviter is None or inviter.is_admin
                 or inviter.referral_rewards >= pricing.REFERRAL_MAX_REWARDS)
    if not no_reward and inviter.stripe_customer_id:
        same_card = set(check.get("fingerprints") or []) & set(await billing.card_fingerprints(inviter.stripe_customer_id))
        no_reward = bool(same_card)                  # paid with the inviter's own card: not a friend
    if not await store.claim_job("referral-reward", friend.user_id):
        return "skipped"                             # another copy of the server decided already
    friend.referral_done = True
    await store.upsert_user(friend)
    if no_reward:
        return "skipped"
    inviter.referral_rewards += 1
    inviter.referral_pending += 1
    await store.upsert_user(inviter)
    return "rewarded"


async def sweep(store, billing, now: float | None = None) -> dict:
    """Settle rewards whose hold has passed, then apply any credit that is waiting."""
    now = time.time() if now is None else now
    report = {"rewarded": 0, "skipped": 0, "waiting": 0, "applied": 0}
    if not pricing.referral_enabled():
        return report
    hold = pricing.REFERRAL_HOLD_DAYS * DAY
    inviters: set[str] = set()
    for listed in await store.list_users():
        if listed.referral_pending > 0:
            inviters.add(listed.user_id)
        if not (listed.referred_by and listed.referral_paid_at and not listed.referral_done
                and listed.stripe_customer_id and now >= listed.referral_paid_at + hold):
            continue
        friend = await store.get_user(listed.user_id)
        if friend is None or friend.referral_done:
            continue
        outcome = await _settle(store, billing, friend, now)
        report[outcome] += 1
        if outcome == "rewarded":
            inviters.add(friend.referred_by)
    for user_id in inviters:
        report["applied"] += await apply_pending(store, billing, user_id)
    return report
