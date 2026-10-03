"""Deep health checks for the external watchdog.

Two public endpoints (wired in hosted.py):

  /health/live  no dependencies; answers as long as the server process is up.
  /health/deep  checks the things that can break while the process is still
                "running": the database, the LaTeX engine, and (reported
                separately as "external") the login provider, the payments
                key and the Overleaf Git host.

Why the ours/external split: the watchdog restarts or rolls back for problems in
"ours". A WorkOS, Stripe or Overleaf outage cannot be fixed from our side, so
those are reported (status page, alert email) but never trigger a fix.

Safety: every check has a hard timeout and a cache, so this endpoint can never
become a load problem or a way to hammer a dependency, and it returns only
booleans, timings and short error CLASS names (never messages, keys or URLs).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from typing import Awaitable, Callable

STARTED_AT = time.time()

# (cache seconds) per check. Cheap local checks refresh often, third-party ones
# rarely so a one-minute watchdog does not become a one-minute load on them.
TTL = {"storage": 20, "latex": 600, "signin_keys": 60, "payments": 300, "overleaf_git": 120}
CHECK_TIMEOUT = 8.0
# The path a real git client hits, with a project id that cannot exist: a healthy
# host answers 401 (asks for credentials). The bare homepage always answers 500,
# so it must not be used as the probe.
OVERLEAF_GIT_URL = "https://git.overleaf.com/000000000000000000000000/info/refs?service=git-upload-pack"


def version() -> str:
    return os.environ.get("MILATEXAI_SHA", "dev")


class DeepHealth:
    def __init__(self, store, billing, authkit_domain: str | None, tectonic_path: Callable[[], str | None],
                 http_get: Callable[[str], Awaitable[tuple[int, bytes]]] | None = None) -> None:
        self.store = store
        self.billing = billing
        self.authkit_domain = (authkit_domain or "").rstrip("/")
        self.tectonic_path = tectonic_path
        self._http_get = http_get or self._aiohttp_get
        self._cache: dict[str, tuple[float, dict]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # -- plumbing -------------------------------------------------------------

    @staticmethod
    async def _aiohttp_get(url: str) -> tuple[int, bytes]:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=CHECK_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, allow_redirects=False) as resp:
                return resp.status, await resp.read()

    async def _cached(self, name: str, fn: Callable[[], Awaitable[dict]]) -> dict:
        now = time.monotonic()
        hit = self._cache.get(name)
        if hit and now - hit[0] < TTL[name]:
            return hit[1]
        lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock:  # concurrent callers share one real check
            hit = self._cache.get(name)
            if hit and time.monotonic() - hit[0] < TTL[name]:
                return hit[1]
            started = time.monotonic()
            try:
                result = await asyncio.wait_for(fn(), CHECK_TIMEOUT + 2)
            except asyncio.TimeoutError:
                result = {"ok": False, "error": "Timeout"}
            except Exception as exc:  # noqa: BLE001  (class name only, never the message)
                result = {"ok": False, "error": type(exc).__name__}
            result["ms"] = int((time.monotonic() - started) * 1000)
            self._cache[name] = (time.monotonic(), result)
            return result

    # -- the checks -----------------------------------------------------------

    async def _storage(self) -> dict:
        # A point read of an id that does not exist: proves the connection and
        # the credentials work without touching any real record.
        await self.store.get_user("__health_probe__")
        return {"ok": True}

    async def _latex(self) -> dict:
        exe = self.tectonic_path()
        if not exe:
            return {"ok": False, "error": "EngineMissing"}

        def run() -> int:
            return subprocess.run([exe, "--version"], capture_output=True, timeout=10).returncode

        code = await asyncio.to_thread(run)
        return {"ok": code == 0} if code == 0 else {"ok": False, "error": "EngineFailed"}

    async def _signin_keys(self) -> dict:
        if not self.authkit_domain:
            return {"ok": True, "skipped": True}
        status, body = await self._http_get(f"{self.authkit_domain}/oauth2/jwks")
        if status != 200:
            return {"ok": False, "error": f"HTTP{status}"}
        import json

        keys = json.loads(body).get("keys")
        return {"ok": bool(keys)} if keys else {"ok": False, "error": "NoKeys"}

    async def _payments(self) -> dict:
        if not getattr(self.billing, "enabled", False):
            return {"ok": True, "skipped": True}

        def run() -> None:
            self.billing._stripe().Account.retrieve()

        await asyncio.to_thread(run)
        return {"ok": True}

    async def _overleaf_git(self) -> dict:
        # An auth challenge (401) or "no such project" (404) proves the git host is
        # answering; only a server error or no answer at all counts as down.
        status, _ = await self._http_get(OVERLEAF_GIT_URL)
        return {"ok": status < 500} if status < 500 else {"ok": False, "error": f"HTTP{status}"}

    # -- public ---------------------------------------------------------------

    async def run(self) -> dict:
        ours = {"storage": self._storage, "latex": self._latex}
        external = {"signin_keys": self._signin_keys, "payments": self._payments,
                    "overleaf_git": self._overleaf_git}
        names = list(ours) + list(external)
        results = await asyncio.gather(*(self._cached(n, {**ours, **external}[n]) for n in names))
        checks = {n: {**r, "scope": "ours" if n in ours else "external"} for n, r in zip(names, results)}
        return {
            "ok": all(c["ok"] for c in checks.values() if c["scope"] == "ours"),
            "degraded": any(not c["ok"] for c in checks.values() if c["scope"] == "external"),
            "version": version(),
            "uptime_seconds": int(time.time() - STARTED_AT),
            "checks": checks,
        }
