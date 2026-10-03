"""Deep health checks used by the external watchdog."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest
from starlette.testclient import TestClient

from leafbridge import health
from leafbridge.capacity import CapacityGate
from leafbridge.health import DeepHealth
from leafbridge.hosted import create_hosted_server
from leafbridge.store import InMemoryStore, TokenCipher


class _Billing:
    def __init__(self, enabled=False, fail=False):
        self.enabled, self._fail = enabled, fail

    def _stripe(self):
        outer = self

        class Account:
            @staticmethod
            def retrieve():
                if outer._fail:
                    raise RuntimeError("secret detail that must never leak")

        class S:
            pass

        S.Account = Account
        return S


def _http(jwks_status=200, git_status=401, calls=None):
    async def get(url):
        if calls is not None:
            calls.append(url)
        if "jwks" in url:
            return jwks_status, json.dumps({"keys": [{"kid": "k"}]}).encode()
        return git_status, b""
    return get


def _dh(store=None, billing=None, http=None, tectonic=sys.executable, domain="https://auth.example"):
    return DeepHealth(store or InMemoryStore(), billing or _Billing(), domain, lambda: tectonic,
                      http_get=http or _http())


def run(dh):
    return asyncio.run(dh.run())


@pytest.fixture(autouse=True)
def _fresh_clock_and_ttl(monkeypatch):
    monkeypatch.setattr(health, "TTL", {k: 0 for k in health.TTL})  # no caching unless a test opts in


def test_everything_healthy():
    r = run(_dh(billing=_Billing(enabled=True)))
    assert r["ok"] is True and r["degraded"] is False
    assert set(r["checks"]) == {"storage", "latex", "signin_keys", "payments", "overleaf_git"}
    assert all(c["ok"] for c in r["checks"].values())
    assert {c["scope"] for c in r["checks"].values()} == {"ours", "external"}


def test_storage_failure_is_ours_and_marks_not_ok():
    class Boom(InMemoryStore):
        async def get_user(self, user_id):
            raise ConnectionError("connection string with a SECRET in it")

    r = run(_dh(store=Boom()))
    assert r["ok"] is False
    assert r["checks"]["storage"]["error"] == "ConnectionError"  # the class name only
    assert "SECRET" not in json.dumps(r)


def test_missing_latex_engine_is_ours():
    r = run(_dh(tectonic=None))
    assert r["ok"] is False and r["checks"]["latex"]["error"] == "EngineMissing"


def test_third_party_outage_is_degraded_not_down():
    r = run(_dh(billing=_Billing(enabled=True, fail=True), http=_http(jwks_status=503, git_status=502)))
    assert r["ok"] is True  # nothing of ours is broken
    assert r["degraded"] is True
    bad = {n for n, c in r["checks"].items() if not c["ok"]}
    assert bad == {"signin_keys", "payments", "overleaf_git"}
    assert "secret detail" not in json.dumps(r)


def test_unconfigured_dependencies_are_skipped_not_failed():
    r = run(_dh(domain="", billing=_Billing(enabled=False)))
    assert r["checks"]["signin_keys"].get("skipped") and r["checks"]["payments"].get("skipped")
    assert r["ok"] is True and r["degraded"] is False


def test_results_are_cached(monkeypatch):
    monkeypatch.setattr(health, "TTL", {k: 600 for k in health.TTL})
    calls = []
    dh = _dh(http=_http(calls=calls))
    run(dh)
    first = len(calls)
    run(dh)
    assert first > 0 and len(calls) == first  # second call served from cache, no new network


def test_a_hung_check_times_out_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(health, "CHECK_TIMEOUT", 0.1)

    async def slow(url):
        await asyncio.sleep(5)
        return 200, b"{}"

    r = run(_dh(http=slow))
    assert r["checks"]["overleaf_git"]["error"] == "Timeout"
    assert r["ok"] is True  # it is external, so ours is still fine


def test_routes_live_and_deep(monkeypatch):
    async def fake_get(url):
        return (200, json.dumps({"keys": [1]}).encode()) if "jwks" in url else (401, b"")

    monkeypatch.setattr(DeepHealth, "_aiohttp_get", staticmethod(fake_get))
    # CI and dev machines have no LaTeX engine; the production image does.
    monkeypatch.setattr("leafbridge.texcompile.tectonic_path", lambda: sys.executable)
    mcp = create_hosted_server(
        store=InMemoryStore(), cipher=TokenCipher(TokenCipher.generate_key()), auth=False,
        identity_provider=lambda: ("u", "e"), base_url="https://milatexai.com",
        capacity=CapacityGate(subscription_id="", resource_group="", stripe_api_key=""),
    )
    with TestClient(mcp.http_app()) as client:
        live = client.get("/health/live")
        assert live.status_code == 200 and live.json() == {"ok": True}
        assert live.headers["cache-control"] == "no-store"
        deep = client.get("/health/deep")
        body = deep.json()
        assert deep.status_code == 200 and body["ok"] is True
        assert "version" in body and deep.headers["cache-control"] == "no-store"
