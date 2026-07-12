"""Revenue-backed capacity gating.

Paid users and the admin are ALWAYS served. Free users are served on a
best-effort basis: allowed only while month-to-date infrastructure spend stays
under ``starter + fraction × month-to-date revenue``. At $0 revenue free works up
to ``starter`` (the owner's out-of-pocket willingness); as Pro revenue grows the
free ceiling grows with it, so free access expands "as capacity allows" while the
owner always keeps ``1 - fraction`` of revenue as margin.

Spend comes from Azure Cost Management (via the Container App's managed identity,
which holds Cost Management Reader); revenue from Stripe. Both are in the Stripe/
Azure settlement currency (CAD here), so ``starter`` is in that same currency.
Values are cached (~15 min) because the cost query is slow and rate-limited, and
the gate fails OPEN (serve free users) if the signals can't be fetched — the
manual kill-switch remains the hard backstop.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import time
from dataclasses import dataclass

import aiohttp

_AZURE_MGMT = "https://management.azure.com"
_COST_API_VERSION = "2023-11-01"
DEFAULT_STARTER = float(os.environ.get("FREE_CAPACITY_STARTER", "3"))
DEFAULT_FRACTION = float(os.environ.get("PAID_REVENUE_FRACTION", "0.8"))
DEFAULT_TTL = 900.0  # seconds


@dataclass
class CapacitySnapshot:
    spend: float
    revenue: float
    free_ceiling: float
    free_open: bool
    fresh: bool  # True if the last refresh succeeded (vs stale/fail-open fallback)
    age: float


class CapacityGate:
    """Caches (spend, revenue) and decides whether free users may be served."""

    def __init__(
        self,
        *,
        subscription_id: str,
        resource_group: str,
        stripe_api_key: str,
        starter: float = DEFAULT_STARTER,
        fraction: float = DEFAULT_FRACTION,
        ttl: float = DEFAULT_TTL,
    ):
        self.subscription_id = subscription_id
        self.resource_group = resource_group
        self.stripe_api_key = stripe_api_key
        self.starter = starter
        self.fraction = fraction
        self.ttl = ttl
        # Gating needs the Azure subscription to read spend. Without it we can't
        # measure capacity, so we don't gate (free always allowed).
        self.enabled = bool(subscription_id)
        self._snap: CapacitySnapshot | None = None
        self._lock = asyncio.Lock()

    async def free_allowed(self) -> bool:
        if not self.enabled:
            return True
        snap = await self.snapshot()
        return snap.free_open

    async def snapshot(self) -> CapacitySnapshot:
        if not self.enabled:
            return CapacitySnapshot(0.0, 0.0, self.starter, True, False, 0.0)
        now = time.monotonic()
        snap = self._snap
        if snap is not None and (now - self._snap_at) < self.ttl:
            return snap
        async with self._lock:
            # Re-check after acquiring the lock (another task may have refreshed).
            if self._snap is not None and (time.monotonic() - self._snap_at) < self.ttl:
                return self._snap
            self._snap = await self._refresh()
            self._snap_at = time.monotonic()
            return self._snap

    async def _refresh(self) -> CapacitySnapshot:
        try:
            spend, revenue = await asyncio.gather(
                self._azure_spend(), self._stripe_revenue()
            )
            ceiling = self.starter + self.fraction * revenue
            return CapacitySnapshot(spend, revenue, ceiling, spend < ceiling, True, 0.0)
        except Exception:
            # Keep the last known reading if we have one; otherwise fail OPEN so a
            # transient outage never wrongly denies free users.
            if self._snap is not None:
                s = self._snap
                return CapacitySnapshot(s.spend, s.revenue, s.free_ceiling, s.free_open, False, 0.0)
            return CapacitySnapshot(0.0, 0.0, self.starter, True, False, 0.0)

    # -- signal sources -----------------------------------------------------

    async def _azure_spend(self) -> float:
        """Month-to-date actual cost for the resource group (settlement currency)."""
        from azure.identity.aio import DefaultAzureCredential

        cred = DefaultAzureCredential()
        try:
            token = (await cred.get_token(f"{_AZURE_MGMT}/.default")).token
        finally:
            await cred.close()
        url = (
            f"{_AZURE_MGMT}/subscriptions/{self.subscription_id}/resourceGroups/"
            f"{self.resource_group}/providers/Microsoft.CostManagement/query"
            f"?api-version={_COST_API_VERSION}"
        )
        body = {
            "type": "ActualCost",
            "timeframe": "MonthToDate",
            "dataset": {
                "granularity": "None",
                "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            },
        }
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(
                url, json=body, headers={"Authorization": f"Bearer {token}"}
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        rows = (data.get("properties") or {}).get("rows") or []
        return float(rows[0][0]) if rows and rows[0] else 0.0

    async def _stripe_revenue(self) -> float:
        """Month-to-date gross revenue from Stripe (settlement currency)."""
        import stripe

        stripe.api_key = self.stripe_api_key
        now = datetime.datetime.now(datetime.timezone.utc)
        month_start = int(
            datetime.datetime(now.year, now.month, 1, tzinfo=datetime.timezone.utc).timestamp()
        )

        def _work() -> float:
            total = 0
            for bt in stripe.BalanceTransaction.list(
                created={"gte": month_start}, type="charge", limit=100
            ).auto_paging_iter():
                total += bt.amount  # minor units, settlement currency, gross
            return total / 100.0

        return await asyncio.to_thread(_work)

    # snapshot timestamp lives outside the dataclass so a stale snap can be reused
    _snap_at: float = 0.0

    @classmethod
    def from_env(cls, stripe_api_key: str | None = None) -> "CapacityGate":
        return cls(
            subscription_id=os.environ.get("AZURE_SUBSCRIPTION_ID", ""),
            resource_group=os.environ.get("AZURE_COST_RG", "milatexai-rg"),
            stripe_api_key=stripe_api_key or os.environ.get("STRIPE_SECRET_KEY", ""),
            starter=float(os.environ.get("FREE_CAPACITY_STARTER", DEFAULT_STARTER)),
            fraction=float(os.environ.get("PAID_REVENUE_FRACTION", DEFAULT_FRACTION)),
        )
