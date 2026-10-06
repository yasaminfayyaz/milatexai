"""Who goes first when the server is busy.

Compiling LaTeX, rendering pages and building diffs are the expensive part of the
service: each one keeps a CPU busy for seconds. Each copy of the server runs at
most ``HEAVY_SLOTS`` of them at once (more would only make every one slower), and
the copies scale out when they stay busy.

While a copy is full:
  - Pro users and admins wait their turn ahead of free users, and are never turned
    away: after ``PRO_MAX_WAIT`` seconds they run anyway, even over the limit.
  - Free users wait up to ``FREE_MAX_WAIT`` seconds, then get a friendly "busy,
    try again in a minute" message.

Free users also have an hourly allowance of heavy jobs (``FREE_HEAVY_PER_HOUR``),
counted in shared storage so it holds across copies, so one free account cannot
keep the service scaled out on its own.

Who is calling is set per request by :func:`set_caller` (from the capacity check
that every project tool runs). Code that never sets it, like the local
single-user server, is treated as a priority caller and never refused.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

HEAVY_SLOTS = max(1, int(os.environ.get("HEAVY_SLOTS", "2")))
FREE_MAX_WAIT = float(os.environ.get("FREE_MAX_WAIT", "20"))
PRO_MAX_WAIT = float(os.environ.get("PRO_MAX_WAIT", "120"))
FREE_HEAVY_PER_HOUR = int(os.environ.get("FREE_HEAVY_PER_HOUR", "40"))


class Busy(Exception):
    """A free request could not get a turn in time, or used up its hourly allowance."""


@dataclass(frozen=True)
class Caller:
    priority: bool
    user_id: str = ""
    store: object | None = None


_caller: contextvars.ContextVar[Caller | None] = contextvars.ContextVar("milatexai_caller", default=None)


def set_caller(*, priority: bool, user_id: str = "", store=None) -> None:
    _caller.set(Caller(priority=priority, user_id=user_id, store=store))


def current() -> Caller:
    return _caller.get() or Caller(priority=True)


def hour_key(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H", time.gmtime(now if now is not None else time.time()))


class HeavySlots:
    def __init__(self, slots: int = HEAVY_SLOTS, *, free_wait: float = FREE_MAX_WAIT,
                 pro_wait: float = PRO_MAX_WAIT, free_per_hour: int = FREE_HEAVY_PER_HOUR):
        self.slots = slots
        self.free_wait = free_wait
        self.pro_wait = pro_wait
        self.free_per_hour = free_per_hour
        self.busy = 0
        self.priority_waiting = 0
        self._cond: asyncio.Condition | None = None

    def _condition(self) -> asyncio.Condition:
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    async def _check_hourly(self, caller: Caller) -> None:
        if caller.priority or not caller.user_id or caller.store is None or self.free_per_hour <= 0:
            return
        try:
            used = await caller.store.increment_usage(f"heavy:{caller.user_id}", hour_key())
        except Exception:  # noqa: BLE001  (a storage hiccup must not block anyone)
            return
        if used > self.free_per_hour:
            raise Busy(
                f"You've used this hour's {self.free_per_hour} compiles and previews on the free plan. "
                "They come back at the top of the hour. Reading and editing files still works, and "
                "Pro has no limit (run `upgrade`)."
            )

    @asynccontextmanager
    async def slot(self):
        caller = current()
        await self._check_hourly(caller)
        cond = self._condition()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (self.pro_wait if caller.priority else self.free_wait)
        async with cond:
            if caller.priority:
                self.priority_waiting += 1
            try:
                # A free request also yields to any paying request already waiting.
                while self.busy >= self.slots or (not caller.priority and self.priority_waiting > 0):
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        if caller.priority:
                            break         # never turn a paying user away: run over the limit
                        raise Busy(
                            "MiLatexAI is busy right now. Please try again in a minute. "
                            "(Pro requests are always served first.)"
                        )
                    try:
                        await asyncio.wait_for(cond.wait(), remaining)
                    except asyncio.TimeoutError:
                        pass
            finally:
                if caller.priority:
                    self.priority_waiting -= 1
            self.busy += 1
        try:
            yield
        finally:
            async with cond:
                self.busy -= 1
                cond.notify_all()


_slots = HeavySlots()


async def run_heavy(fn, *args, **kwargs):
    """Run a CPU-heavy function in a worker thread once this request gets a turn."""
    async with _slots.slot():
        return await asyncio.to_thread(fn, *args, **kwargs)
