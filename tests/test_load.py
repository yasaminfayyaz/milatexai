"""Who goes first when the server is busy (leafbridge/load.py).

The promise to paying users is that they are never turned away. These tests pin it:
Pro waits ahead of free, Pro runs even when every slot stays taken, free gets a
clear "busy" message instead of hanging, and the free hourly allowance holds.
"""

from __future__ import annotations

import asyncio

import pytest

from leafbridge import load
from leafbridge.store import InMemoryStore


def run(coro):
    return asyncio.run(coro)


async def hold(slots: load.HeavySlots, seconds: float, started: asyncio.Event | None = None, *, priority=True):
    load.set_caller(priority=priority, user_id="holder")
    async with slots.slot():
        if started:
            started.set()
        await asyncio.sleep(seconds)


def test_a_caller_nobody_identified_is_treated_as_priority():
    assert load.current().priority is True


def test_free_gets_a_clear_busy_message_when_every_slot_stays_taken():
    async def go():
        slots = load.HeavySlots(1, free_wait=0.2, pro_wait=5)
        started = asyncio.Event()
        holder = asyncio.create_task(hold(slots, 1.0, started))
        await started.wait()
        load.set_caller(priority=False, user_id="free1")
        with pytest.raises(load.Busy, match="busy right now"):
            async with slots.slot():
                pass
        holder.cancel()
    run(go())


def test_pro_is_never_turned_away_even_when_every_slot_stays_taken():
    async def go():
        slots = load.HeavySlots(1, free_wait=0.1, pro_wait=0.2)
        started = asyncio.Event()
        holder = asyncio.create_task(hold(slots, 2.0, started))
        await started.wait()
        load.set_caller(priority=True, user_id="pro1")
        ran = False
        async with slots.slot():          # runs over the limit after waiting pro_wait
            ran = True
        holder.cancel()
        return ran
    assert run(go()) is True


def test_pro_waiting_is_served_before_free_that_was_waiting_longer():
    async def go():
        slots = load.HeavySlots(1, free_wait=5, pro_wait=5)
        order: list[str] = []
        started = asyncio.Event()

        async def worker(name: str, priority: bool, delay: float):
            await asyncio.sleep(delay)
            load.set_caller(priority=priority, user_id=name)
            async with slots.slot():
                order.append(name)
                await asyncio.sleep(0.05)

        holder = asyncio.create_task(hold(slots, 0.3, started))
        await started.wait()
        free = asyncio.create_task(worker("free", False, 0.0))     # starts waiting first
        pro = asyncio.create_task(worker("pro", True, 0.1))        # arrives later
        await asyncio.gather(holder, free, pro)
        return order
    assert run(go()) == ["pro", "free"]


def test_slots_free_up_and_are_reused():
    async def go():
        slots = load.HeavySlots(2, free_wait=2, pro_wait=2)
        load.set_caller(priority=False, user_id="f")
        done = 0

        async def job():
            nonlocal done
            async with slots.slot():
                await asyncio.sleep(0.02)
                done += 1
        await asyncio.gather(*(job() for _ in range(10)))
        return done, slots.busy, slots.priority_waiting
    assert run(go()) == (10, 0, 0)


def test_an_exception_inside_a_slot_still_releases_it():
    async def go():
        slots = load.HeavySlots(1, free_wait=0.5, pro_wait=0.5)
        load.set_caller(priority=False, user_id="f")
        with pytest.raises(ValueError):
            async with slots.slot():
                raise ValueError("compile crashed")
        async with slots.slot():           # would time out with Busy if the slot leaked
            return slots.busy
    assert run(go()) == 1


def test_free_hourly_allowance_is_counted_in_shared_storage_and_spares_pro():
    async def go():
        store = InMemoryStore()
        slots = load.HeavySlots(3, free_per_hour=3)
        load.set_caller(priority=False, user_id="free1", store=store)
        for _ in range(3):
            async with slots.slot():
                pass
        with pytest.raises(load.Busy, match="this hour"):
            async with slots.slot():
                pass
        load.set_caller(priority=True, user_id="pro1", store=store)
        for _ in range(10):
            async with slots.slot():
                pass
        load.set_caller(priority=False, user_id="free2", store=store)   # another free user has their own allowance
        async with slots.slot():
            pass
        return await store.get_usage("heavy:free1", load.hour_key()), await store.get_usage("heavy:pro1", load.hour_key())
    assert run(go()) == (4, 0)


def test_a_storage_failure_never_blocks_anyone():
    class Down:
        async def increment_usage(self, *a, **k):
            raise RuntimeError("storage down")

    async def go():
        slots = load.HeavySlots(1, free_per_hour=1)
        load.set_caller(priority=False, user_id="f", store=Down())
        async with slots.slot():
            return True
    assert run(go()) is True


def test_callers_do_not_leak_between_concurrent_requests():
    async def go():
        seen = {}

        async def request(name, priority):
            load.set_caller(priority=priority, user_id=name)
            await asyncio.sleep(0.01)
            seen[name] = load.current().priority
        await asyncio.gather(request("a", True), request("b", False), request("c", True))
        return seen
    assert run(go()) == {"a": True, "b": False, "c": True}


def test_run_heavy_runs_the_function_off_the_event_loop():
    assert run(load.run_heavy(lambda x: x * 2, 21)) == 42


def test_hosted_capacity_check_marks_the_caller():
    import os
    os.environ.setdefault("WORKOS_AUTHKIT_DOMAIN", "https://placeholder.authkit.invalid")
    from pathlib import Path
    import tempfile
    from leafbridge.hosted import HostedApp
    from leafbridge.store import TokenCipher, User

    store = InMemoryStore()
    app = HostedApp(store=store, cipher=TokenCipher(TokenCipher.generate_key()), data_dir=Path(tempfile.mkdtemp()))

    async def go():
        await app.ensure_capacity(User(user_id="f", email="f@x.com", plan="free"))
        free = load.current()
        await app.ensure_capacity(User(user_id="p", email="p@x.com", plan="pro"))
        pro = load.current()
        await app.ensure_capacity(User(user_id="a", email="a@x.com", is_admin=True))
        admin = load.current()
        return free, pro, admin
    free, pro, admin = run(go())
    assert (free.priority, free.user_id) == (False, "f") and free.store is store
    assert pro.priority is True and admin.priority is True


def test_busy_reaches_the_user_as_a_plain_message():
    from leafbridge.hosted import _wrap
    err = _wrap(load.Busy("MiLatexAI is busy right now. Please try again in a minute."))
    assert "busy right now" in str(err) and "Unexpected" not in str(err)
