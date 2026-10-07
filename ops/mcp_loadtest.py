"""Compile load test: simulated people using MiLatexAI's real tools, against the THROWAWAY copy.

Each simulated person signs in with a test-only token (signed by the load-test key that only
milatexai-drill trusts), connects the synthetic 20-page paper, then works like a real user:
compile, look at pages and tables, read sections, with a few seconds of thinking in between.
Half are Pro (admin emails on the test copy), half are free. The number of people grows in
steps; for each step the report gives response times for Pro and free separately, how often
free users were told "busy", any real errors, and how many copies were running.

Run by .github/workflows/compile-load-test.yml. Environment:
  BASE_URL, ISSUER, ISSUER_KEY (PEM), REPO_URL, REPO_TOKEN, APP, STEPS ("2,4,8"), STEP_SECONDS
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import shutil
import subprocess
import time
from collections import defaultdict

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

BASE = os.environ["BASE_URL"].rstrip("/")
ISSUER = os.environ["ISSUER"]
KEY = serialization.load_pem_private_key(os.environ["ISSUER_KEY"].encode(), password=None)
REPO_URL = os.environ["REPO_URL"]
REPO_TOKEN = os.environ["REPO_TOKEN"]
APP = os.environ.get("APP", "milatexai-drill")
STEPS = [int(x) for x in os.environ.get("STEPS", "2,4,8,12,16,20").split(",")]
STEP_SECONDS = int(os.environ.get("STEP_SECONDS", "180"))
EDITS = os.environ.get("EDITS", "1") == "1"     # authors edit their own note file (needs a write token)
THINK = (3.0, 8.0)
HEAVY = {"check_compile", "show_page", "show_table"}
T0 = time.time()

calls: list[dict] = []        # one record per tool call
copies: list[tuple] = []      # (seconds since start, step users, copies)
current_step = {"users": 0}


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def token(i: int) -> tuple[str, str]:
    tier = "pro" if i % 2 == 0 else "free"
    email = f"{tier}-{i}@loadtest.invalid"
    now = int(time.time())
    claims = {"iss": ISSUER, "sub": f"lt-user-{i}", "email": email, "aud": BASE + "/mcp", "iat": now, "exp": now + 4 * 3600}
    head = b64(json.dumps({"alg": "RS256", "typ": "JWT", "kid": "loadtest-1"}).encode())
    body = b64(json.dumps(claims).encode())
    sig = KEY.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{b64(sig)}", tier


class User:
    def __init__(self, i: int, session: aiohttp.ClientSession):
        self.i = i
        self.session = session
        self.jwt, self.tier = token(i)
        self.protocol = None
        self.rid = 0

    async def rpc(self, method: str, params: dict) -> tuple[int, dict | None]:
        self.rid += 1
        headers = {"Authorization": f"Bearer {self.jwt}", "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        body = {"jsonrpc": "2.0", "id": self.rid, "method": method, "params": params}
        async with self.session.post(BASE + "/mcp", json=body, headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=300)) as r:
            raw = await r.text()
            if r.status != 200:
                return r.status, None
            if "text/event-stream" in (r.headers.get("Content-Type") or ""):
                data = [l[5:].strip() for l in raw.splitlines() if l.startswith("data:")]
                raw = data[-1] if data else "{}"
            return 200, json.loads(raw or "{}")

    async def tool(self, name: str, args: dict) -> None:
        started = time.monotonic()
        kind, text = "ok", ""
        try:
            status, reply = await self.rpc("tools/call", {"name": name, "arguments": args})
            if status != 200 or reply is None:
                kind = f"http{status}"
            else:
                result = reply.get("result") or {}
                text = " ".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
                if reply.get("error"):
                    kind, text = "rpc_error", str(reply["error"])[:200]
                elif result.get("isError"):
                    kind = ("busy" if "busy right now" in text else "hourly" if "this hour" in text
                            else "limit" if "free commits" in text else "tool_error")
        except Exception as exc:  # noqa: BLE001
            kind, text = type(exc).__name__, str(exc)[:200]
        calls.append({"t": time.time() - T0, "step": current_step["users"], "user": self.i, "tier": self.tier,
                      "tool": name, "kind": kind, "s": time.monotonic() - started, "note": text[:160] if kind != "ok" else ""})

    async def setup(self) -> bool:
        status, reply = await self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                      "clientInfo": {"name": "milatexai-loadtest", "version": "1"}})
        if status != 200 or not reply:
            calls.append({"t": time.time() - T0, "step": current_step["users"], "user": self.i, "tier": self.tier,
                          "tool": "initialize", "kind": f"http{status}", "s": 0, "note": ""})
            return False
        self.protocol = reply["result"]["protocolVersion"]
        _, listed = await self.rpc("tools/call", {"name": "list_projects", "arguments": {}})
        text = json.dumps(listed or {})
        if "paper" not in text:
            await self.tool("connect_project", {"overleaf_url": REPO_URL, "token": REPO_TOKEN, "name": "paper"})
        return True

    async def work(self, stop: asyncio.Event) -> None:
        if not await self.setup():
            return
        actions = [("check_compile", lambda: {}, 30), ("show_page", lambda: {"page": random.randint(1, 12)}, 20),
                   ("show_table", lambda: {"table": str(random.randint(1, 6))}, 15),
                   ("read_file", lambda: {"path": f"sections/s{random.randint(1, 8)}.tex"}, 20),
                   ("get_sections", lambda: {"path": "main.tex"}, 15)]
        names, makers, weights = zip(*[(a, m, w) for a, m, w in actions])
        notes: list[str] = []
        just_edited = False
        while not stop.is_set():
            if just_edited and random.random() < 0.7:
                await self.tool("check_compile", {"project": "paper"})       # people check right after editing
                just_edited = False
            elif EDITS and random.random() < 0.2:
                # A real commit and push: this author's own note file (included in the paper).
                notes.append(f"Note {len(notes) + 1} from author {self.i}: the {random.choice(['method', 'result', 'table', 'proof'])} needs another look.")
                body = "".join(n + chr(10) + chr(10) for n in notes)
                await self.tool("write_file", {"path": f"notes/u{self.i}.tex", "content": body, "project": "paper",
                                               "allow_shrink": True})
                just_edited = True
            else:
                k = random.choices(range(len(names)), weights=weights)[0]
                await self.tool(names[k], {**makers[k](), "project": "paper"})
            try:
                await asyncio.wait_for(stop.wait(), random.uniform(*THINK))
            except asyncio.TimeoutError:
                pass


def count_copies() -> int:
    az = shutil.which("az") or "az"
    try:
        out = subprocess.run([az, "containerapp", "replica", "list", "-n", APP, "-g", "milatexai-rg", "--query", "length(@)",
                              "-o", "tsv", "--only-show-errors"], capture_output=True, text=True, timeout=60).stdout.strip()
        return int(out)
    except Exception:  # noqa: BLE001
        return -1


async def watch(stop: asyncio.Event) -> None:
    while not stop.is_set():
        n = await asyncio.to_thread(count_copies)
        copies.append((int(time.time() - T0), current_step["users"], n))
        print(json.dumps({"t": int(time.time() - T0), "users": current_step["users"], "copies": n, "calls": len(calls),
                          "not_ok": sum(1 for c in calls if c["kind"] != "ok")}), flush=True)
        try:
            await asyncio.wait_for(stop.wait(), 20)
        except asyncio.TimeoutError:
            pass


def pct(xs: list[float], p: float):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(len(xs) * p))], 1) if xs else None


def summarize() -> dict:
    steps = []
    for n in STEPS:
        rows = [c for c in calls if c["step"] == n]
        tiers = {}
        for tier in ("pro", "free"):
            rs = [c for c in rows if c["tier"] == tier]
            heavy = [c["s"] for c in rs if c["tool"] in HEAVY and c["kind"] == "ok"]
            writes = [c["s"] for c in rs if c["tool"] == "write_file" and c["kind"] == "ok"]
            light = [c["s"] for c in rs if c["tool"] not in HEAVY and c["kind"] == "ok"]
            kinds = defaultdict(int)
            for c in rs:
                kinds[c["kind"]] += 1
            tiers[tier] = {"calls": len(rs), "outcomes": dict(kinds), "compile_p50_s": pct(heavy, .5),
                           "compile_p95_s": pct(heavy, .95), "compile_max_s": pct(heavy, 1.0), "light_p95_s": pct(light, .95),
                           "edits_ok": len(writes), "edit_p95_s": pct(writes, .95),
                           "instant_share": round(sum(1 for x in heavy if x < 5) / len(heavy), 2) if heavy else None}
        cps = [c for (_, u, c) in copies if u == n and c >= 0]
        steps.append({"users": n, "copies_min": min(cps) if cps else None, "copies_max": max(cps) if cps else None, **tiers})
    errors = [c for c in calls if c["kind"] not in ("ok", "busy", "hourly", "limit")]
    return {"summary": True, "steps": steps, "real_errors": len(errors),
            "error_examples": [{k: e[k] for k in ("tool", "tier", "kind", "note")} for e in errors[:8]],
            "pro_not_served": sum(1 for c in calls if c["tier"] == "pro" and c["kind"] != "ok")}


async def main() -> None:
    connector = aiohttp.TCPConnector(limit=200, ttl_dns_cache=3600)
    async with aiohttp.ClientSession(connector=connector, headers={"user-agent": "milatexai-loadtest"}) as session:
        stop = asyncio.Event()
        watcher = asyncio.create_task(watch(stop))
        tasks: list[asyncio.Task] = []
        for n in STEPS:
            current_step["users"] = n
            while len(tasks) < n:
                tasks.append(asyncio.create_task(User(len(tasks), session).work(stop)))
            print(json.dumps({"step": n, "started": True}), flush=True)
            await asyncio.sleep(STEP_SECONDS)
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await watcher
    print(json.dumps(summarize()), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
