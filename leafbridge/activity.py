"""How often each account uses each tool, per day. Counts only.

What is kept: for an account, a UTC day and a tool name, how many times that tool was called
and how many of those calls ended in an error. Never a file name, path, project name, argument
or any content. Rows older than RETAIN_DAYS are deleted.

How it stays cheap and out of the way:
  * A tool call only bumps an in-memory counter (no network, cannot fail the call).
  * Each running copy writes its totals to storage about once a minute, as rows that only that
    copy writes (the row key ends in a random id per process), so copies never contend for the
    same row, unlike the commit counter. Summing the rows gives the real total.
  * A write failure keeps the counts and retries on the next flush.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

log = logging.getLogger("milatexai.activity")

FLUSH_SECONDS = 60
RETAIN_DAYS = 90
MAX_KEYS = 20000        # safety bound on memory; far above a real day's (user, tool) pairs


def day_of(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() if ts is None else ts))


def day_before(days: int, ts: float | None = None) -> str:
    return day_of((time.time() if ts is None else ts) - days * 86400)


class ActivityTracker:
    def __init__(self, store, *, flush_seconds: float = FLUSH_SECONDS, copy_id: str | None = None,
                 clock=time.time) -> None:
        self.store = store
        self.flush_seconds = flush_seconds
        self.copy_id = copy_id or uuid.uuid4().hex[:8]
        self._clock = clock
        # (day, user_id, tool) -> [calls, errors]: running totals for this process, written
        # whole each flush so a repeated write is harmless.
        self._totals: dict[tuple[str, str, str], list[int]] = {}
        self._dirty: set[tuple[str, str, str]] = set()
        self._task: asyncio.Task | None = None
        self._last_prune_day = ""
        self._stopping = False

    # -- the hot path: memory only, never raises ---------------------------------------------

    def note(self, user_id: str, tool: str, ok: bool = True) -> None:
        try:
            if not user_id or not tool or self._stopping:
                return
            key = (day_of(self._clock()), user_id, tool[:80])
            row = self._totals.get(key)
            if row is None:
                if len(self._totals) >= MAX_KEYS:
                    return
                row = self._totals[key] = [0, 0]
            row[0] += 1
            if not ok:
                row[1] += 1
            self._dirty.add(key)
            self._ensure_running()
        except Exception:  # noqa: BLE001  (counting must never get in the way of a tool call)
            pass

    def _ensure_running(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            self._task = asyncio.get_running_loop().create_task(self._loop())
        except RuntimeError:
            self._task = None   # no running loop (sync test); flush() can still be called

    # -- writing --------------------------------------------------------------------------------

    async def _loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self.flush_seconds)
            await self.flush()
            # Totals of past days are final once written; forget them so memory stays small.
            today = day_of(self._clock())
            for key in [k for k in self._totals if k[0] < today and k not in self._dirty]:
                del self._totals[key]

    async def flush(self) -> int:
        """Write every changed total. Returns how many rows were written (0 on failure)."""
        if not self._dirty:
            return 0
        keys = sorted(self._dirty)
        by_user: dict[str, list[tuple[str, str, int, int]]] = {}
        for day, user_id, tool in keys:
            calls, errors = self._totals[(day, user_id, tool)]
            by_user.setdefault(user_id, []).append((day, tool, calls, errors))
        written = 0
        for user_id, rows in by_user.items():
            try:
                await self.store.put_activity(user_id, self.copy_id, rows)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not save tool counts (%s); will retry", type(exc).__name__)
                continue
            for day, tool, _c, _e in rows:
                self._dirty.discard((day, user_id, tool))
            written += len(rows)
        await self._maybe_prune()
        return written

    async def _maybe_prune(self) -> None:
        today = day_of(self._clock())
        if self._last_prune_day == today:
            return
        self._last_prune_day = today
        try:
            await self.store.prune_activity(day_before(RETAIN_DAYS, self._clock()))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not prune old tool counts (%s)", type(exc).__name__)

    async def stop(self) -> None:
        """Final write when the server shuts down (a scale-in or a deploy)."""
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        try:
            await asyncio.wait_for(self.flush(), timeout=20)
        except Exception:  # noqa: BLE001
            pass
