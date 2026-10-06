"""Load test: closed-loop HTTP traffic against one app while recording how many copies run.

Run by .github/workflows/load-test.yml (GitHub has the bandwidth and DNS a laptop lacks).
  python ops/loadtest.py <base_url> <app_name> <workers> <load_seconds> <watch_seconds> <paths,comma,separated>
Phases: load (N workers hammering the paths), then a light probe (1 request/s) while
watching the copies scale back in. Prints a timeline and a JSON summary."""
import asyncio, json, os, shutil, subprocess, sys, time
from collections import Counter

import aiohttp

base, app, workers, load_s, watch_s, paths = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5]), sys.argv[6].split(",")
AZ = shutil.which("az") or "az"
ENV = {**os.environ, "MSYS_NO_PATHCONV": "1"}
t0 = time.time()
timeline = []
stats = {"load": Counter(), "probe": Counter()}
lat = {"load": [], "probe": []}
stop_load = asyncio.Event()


def replicas():
    try:
        out = subprocess.run([AZ, "containerapp", "replica", "list", "-n", app, "-g", "milatexai-rg",
                              "--query", "[].name", "-o", "tsv", "--only-show-errors"],
                             capture_output=True, text=True, timeout=60, env=ENV).stdout
        return len([l for l in out.splitlines() if l.strip()])
    except Exception:
        return -1


async def watcher(total_s):
    while time.time() - t0 < total_s:
        n = await asyncio.to_thread(replicas)
        phase = "load" if not stop_load.is_set() else "cooldown"
        timeline.append((int(time.time() - t0), phase, n))
        print(json.dumps({"t": int(time.time() - t0), "phase": phase, "copies": n,
                          "ok": stats["load"]["200"] + stats["probe"]["200"],
                          "errors": sum(v for k, v in (stats["load"] + stats["probe"]).items() if k != "200")}), flush=True)
        await asyncio.sleep(15)


async def worker(session, i):
    k = i
    while not stop_load.is_set():
        url = base + paths[k % len(paths)]
        k += 1
        s = time.monotonic()
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
                await r.read()
                stats["load"][str(r.status)] += 1
        except Exception as e:
            stats["load"][type(e).__name__] += 1
        lat["load"].append(time.monotonic() - s)


async def prober(session, until):
    while time.time() - t0 < until:
        s = time.monotonic()
        try:
            async with session.get(base + "/health/live", timeout=aiohttp.ClientTimeout(total=30)) as r:
                await r.read()
                stats["probe"][str(r.status)] += 1
        except Exception as e:
            stats["probe"][type(e).__name__] += 1
        lat["probe"].append(time.monotonic() - s)
        await asyncio.sleep(1)


def pct(xs, p):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(len(xs) * p))], 3) if xs else None


async def main():
    total = load_s + watch_s
    conn = aiohttp.TCPConnector(limit=workers + 10, force_close=False, ttl_dns_cache=3600)
    async with aiohttp.ClientSession(connector=conn, headers={"user-agent": "milatexai-loadtest"}) as session:
        w = asyncio.create_task(watcher(total))
        p = asyncio.create_task(prober(session, total))
        tasks = [asyncio.create_task(worker(session, i)) for i in range(workers)]
        await asyncio.sleep(load_s)
        stop_load.set()
        await asyncio.gather(*tasks)
        await asyncio.gather(w, p)
    print(json.dumps({"summary": True, "load_requests": dict(stats["load"]), "probe_requests": dict(stats["probe"]),
                      "load_p50_s": pct(lat["load"], .5), "load_p95_s": pct(lat["load"], .95), "load_max_s": pct(lat["load"], 1.0),
                      "probe_p95_s": pct(lat["probe"], .95), "peak_copies": max((n for _, _, n in timeline), default=None),
                      "timeline": timeline}), flush=True)


asyncio.run(main())
