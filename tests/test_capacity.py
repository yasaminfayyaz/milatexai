"""Tests for revenue-backed capacity gating: the free ceiling logic, fail-open
behaviour, caching, and per-user admission control (paid/admin never gated)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from leafbridge.capacity import CapacityGate
from leafbridge.hosted import HostedApp
from leafbridge.store import InMemoryStore, TokenCipher, User


def _gate(spend: float, revenue: float, *, starter: float = 3.0, fraction: float = 0.8) -> CapacityGate:
    g = CapacityGate(subscription_id="sub", resource_group="rg", stripe_api_key="sk",
                     starter=starter, fraction=fraction)

    async def fake_spend():
        return spend

    async def fake_rev():
        return revenue

    g._azure_spend = fake_spend
    g._stripe_revenue = fake_rev
    return g


# --- free ceiling logic ----------------------------------------------------

def test_free_open_under_starter_at_zero_revenue():
    # ceiling = 3 + 0.8*0 = 3 ; spend 2 < 3 -> open
    assert asyncio.run(_gate(spend=2.0, revenue=0.0).free_allowed()) is True


def test_free_closed_over_starter_at_zero_revenue():
    # ceiling = 3 ; spend 4 -> closed
    assert asyncio.run(_gate(spend=4.0, revenue=0.0).free_allowed()) is False


def test_revenue_raises_the_free_ceiling():
    # ceiling = 3 + 0.8*10 = 11 ; spend 6 < 11 -> open again
    assert asyncio.run(_gate(spend=6.0, revenue=10.0).free_allowed()) is True


def test_snapshot_reports_numbers():
    snap = asyncio.run(_gate(spend=5.0, revenue=5.0).snapshot())
    assert snap.spend == 5.0 and snap.revenue == 5.0
    assert snap.free_ceiling == pytest.approx(3 + 0.8 * 5)  # 7.0
    assert snap.free_open is True and snap.fresh is True


# --- disabled + fail-open --------------------------------------------------

def test_disabled_gate_always_allows():
    g = CapacityGate(subscription_id="", resource_group="rg", stripe_api_key="")
    assert g.enabled is False
    assert asyncio.run(g.free_allowed()) is True


def test_fail_open_when_signals_error():
    g = CapacityGate(subscription_id="sub", resource_group="rg", stripe_api_key="sk")

    async def boom():
        raise RuntimeError("cost api down")

    g._azure_spend = boom
    g._stripe_revenue = boom
    # No prior snapshot -> fail OPEN so a transient outage never denies free users.
    assert asyncio.run(g.free_allowed()) is True


def test_snapshot_is_cached_within_ttl():
    calls = {"n": 0}
    g = CapacityGate(subscription_id="sub", resource_group="rg", stripe_api_key="sk", ttl=10_000)

    async def spend():
        calls["n"] += 1
        return 1.0

    async def rev():
        return 0.0

    g._azure_spend = spend
    g._stripe_revenue = rev
    asyncio.run(g.free_allowed())
    asyncio.run(g.free_allowed())
    assert calls["n"] == 1  # second call served from cache


# --- admission control (paid/admin never gated) ----------------------------

def _app(tmp: Path, gate: CapacityGate) -> HostedApp:
    return HostedApp(
        store=InMemoryStore(),
        cipher=TokenCipher(TokenCipher.generate_key()),
        data_dir=tmp,
        capacity=gate,
    )


def test_admin_and_paid_never_gated_even_over_capacity(tmp_path):
    app = _app(tmp_path, _gate(spend=999.0, revenue=0.0))  # wildly over capacity
    # Neither of these should raise.
    asyncio.run(app.ensure_capacity(User("a", "a@x.com", is_admin=True)))
    asyncio.run(app.ensure_capacity(User("p", "p@x.com", plan="pro")))


def test_free_user_refused_over_capacity(tmp_path):
    app = _app(tmp_path, _gate(spend=999.0, revenue=0.0))
    with pytest.raises(ToolError):
        asyncio.run(app.ensure_capacity(User("f", "f@x.com", plan="free")))


def test_free_user_served_under_capacity(tmp_path):
    app = _app(tmp_path, _gate(spend=1.0, revenue=0.0))
    asyncio.run(app.ensure_capacity(User("f", "f@x.com", plan="free")))  # no raise
