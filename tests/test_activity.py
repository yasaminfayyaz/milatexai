"""Per-account tool-use counts (leafbridge/activity.py): counted per day and tool, written
in the background without ever blocking or failing a tool call, and never holding content."""

from __future__ import annotations

import asyncio
import warnings

import pytest
from fastmcp import Client

from leafbridge import activity
from leafbridge.activity import ActivityTracker
from leafbridge.hosted import create_hosted_server
from leafbridge.store import InMemoryStore

warnings.filterwarnings("ignore", category=DeprecationWarning)


def rows_of(store, user="u1"):
    return {k[1]: v for k, v in store._activity.items() if k[0] == user}


def run(coro):
    return asyncio.run(coro)


def test_counts_are_kept_per_day_and_tool_and_written_whole():
    store = InMemoryStore()
    clock = [1_760_000_000.0]
    t = ActivityTracker(store, copy_id="aa", clock=lambda: clock[0])
    for _ in range(3):
        t.note("u1", "read_file")
    t.note("u1", "read_file", ok=False)
    t.note("u1", "list_files")
    assert run(t.flush()) == 2
    day = activity.day_of(clock[0])
    assert rows_of(store) == {f"{day}|read_file|aa": (4, 1), f"{day}|list_files|aa": (1, 0)}
    t.note("u1", "read_file")
    assert run(t.flush()) == 1                        # only the changed row, written as the new total
    assert rows_of(store)[f"{day}|read_file|aa"] == (5, 1)
    assert run(t.flush()) == 0                        # nothing changed, nothing written


def test_two_copies_write_their_own_rows_so_nothing_collides_or_is_lost():
    store = InMemoryStore()
    a, b = ActivityTracker(store, copy_id="aa"), ActivityTracker(store, copy_id="bb")
    for _ in range(7):
        a.note("u1", "read_file")
    for _ in range(5):
        b.note("u1", "read_file")
    run(a.flush())
    run(b.flush())
    assert sum(calls for calls, _e in rows_of(store).values()) == 12
    assert len(rows_of(store)) == 2


def test_a_failed_write_is_retried_and_never_raises():
    class Flaky(InMemoryStore):
        broken = True

        async def put_activity(self, user_id, copy_id, rows):
            if self.broken:
                raise RuntimeError("storage unavailable: secret detail")
            await super().put_activity(user_id, copy_id, rows)

    store = Flaky()
    t = ActivityTracker(store, copy_id="aa")
    t.note("u1", "read_file")
    assert run(t.flush()) == 0 and rows_of(store) == {}
    store.broken = False
    assert run(t.flush()) == 1 and list(rows_of(store).values()) == [(1, 0)]


def test_counting_ignores_junk_and_is_bounded():
    t = ActivityTracker(InMemoryStore())
    t.note("", "read_file")
    t.note("u1", "")
    t.note(None, "x")        # type: ignore[arg-type]
    assert not t._totals
    t.note("u1", "t" * 500)
    assert len(next(iter(t._totals))[2]) == 80
    for i in range(activity.MAX_KEYS + 50):
        t._totals[("2026-01-01", f"u{i}", "x")] = [1, 0]
    t.note("someone-new", "read_file")        # at the limit: dropped, not an error
    assert ("2026-10-15", "someone-new", "read_file") not in t._totals


def test_old_rows_are_pruned_once_a_day_and_recent_ones_kept():
    store = InMemoryStore()
    now = 1_760_000_000.0
    old, recent = activity.day_before(91, now), activity.day_before(5, now)
    store._activity[("u1", f"{old}|read_file|zz")] = (9, 0)
    store._activity[("u1", f"{recent}|read_file|zz")] = (3, 0)
    t = ActivityTracker(store, copy_id="aa", clock=lambda: now)
    t.note("u1", "read_file")
    run(t.flush())
    keys = {k[1] for k in store._activity}
    assert f"{old}|read_file|zz" not in keys and f"{recent}|read_file|zz" in keys


def test_stop_writes_what_is_left():
    store = InMemoryStore()
    t = ActivityTracker(store, copy_id="aa", flush_seconds=3600)

    async def go():
        t.note("u1", "read_file")        # starts the background loop (an hour between writes)
        await t.stop()
        t.note("u1", "read_file")        # after stop: ignored
        return rows_of(store)

    rows = asyncio.run(go())
    assert list(rows.values()) == [(1, 0)]


# --- through the real tool path -----------------------------------------------------------------

def _server(user=("user_a", "a@example.com")):
    store = InMemoryStore()
    return create_hosted_server(store=store, auth=False, identity_provider=lambda: user), store


def test_every_tool_call_is_counted_for_the_calling_account_only():
    mcp, store = _server()

    async def go():
        async with Client(mcp) as c:
            await c.call_tool("list_projects", {})
            await c.call_tool("list_projects", {})
            await c.call_tool("list_files", {})          # a new user: answered with the connect link
        await mcp.activity.flush()

    asyncio.run(go())
    by_tool = {k.split("|")[1]: v for k, v in rows_of(store, "user_a").items()}
    assert by_tool["list_projects"] == (2, 0)
    assert by_tool["list_files"] == (1, 0)             # the connect link is an answer, not a failure
    assert all(k[0] == "user_a" for k in store._activity)


def test_the_counting_layer_marks_errors_and_skips_unidentified_calls():
    from types import SimpleNamespace

    from fastmcp.exceptions import ToolError

    from leafbridge import hosted

    store = InMemoryStore()
    tracker = ActivityTracker(store, copy_id="aa")
    layer = hosted._CountToolUse(tracker)
    ctx = lambda name: SimpleNamespace(message=SimpleNamespace(name=name))     # noqa: E731

    def call(outcome, identify=True):
        async def call_next(_ctx):
            if identify:
                hosted._call_holder.get()["uid"] = "u1"
            if outcome == "error":
                raise ToolError("nope")
            if outcome == "connect":
                raise hosted.ConnectFirst("link")
            return "fine"
        return call_next

    async def go():
        assert await layer.on_call_tool(ctx("a_tool"), call("ok")) == "fine"
        with pytest.raises(ToolError):
            await layer.on_call_tool(ctx("a_tool"), call("error"))
        with pytest.raises(hosted.ConnectFirst):
            await layer.on_call_tool(ctx("connect_tool"), call("connect"))
        await layer.on_call_tool(ctx("anonymous_tool"), call("ok", identify=False))
        await tracker.flush()

    asyncio.run(go())
    by_tool = {k.split("|")[1]: v for k, v in rows_of(store).items()}
    assert by_tool == {"a_tool": (2, 1), "connect_tool": (1, 0)}      # nothing for the unidentified call


def test_a_storage_outage_never_breaks_a_tool_call():
    class Down(InMemoryStore):
        async def put_activity(self, *a, **k):
            raise RuntimeError("down")

    mcp = create_hosted_server(store=Down(), auth=False, identity_provider=lambda: ("user_a", "a@example.com"))

    async def go():
        async with Client(mcp) as c:
            res = await c.call_tool("list_projects", {})
        await mcp.activity.flush()
        return res

    assert asyncio.run(go()) is not None
