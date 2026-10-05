#!/usr/bin/env python3
"""Traffic to the free client-side tools (/tools, /tools/bibtex, /tools/latex-error-finder),
from Cloudflare's own analytics, because those pages are cached at the edge and most visits
never reach our server.

Prints an aggregate report (counts per page and day, visitors, sources, countries). Individual
addresses are only counted, never printed. Needs a Cloudflare token that may read analytics.

Usage: python traffic.py [days]        Environment: CLOUDFLARE_API_TOKEN
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ZONE = "1378686c9478bd4880bca1288ead3079"
PAGES = {"/tools": "Tools index", "/tools/bibtex": "BibTeX tool", "/tools/latex-error-finder": "LaTeX error finder"}
BOT = re.compile(r"bot|crawl|spider|slurp|headless|python-requests|curl/|wget|go-http|scrapy|monitor|uptime|facebookexternalhit|preview|"
                 r"gptbot|claudebot|perplexity|bytespider|semrush|ahrefs|mj12|dataforseo|petalbot|yandex|bingpreview|axios|node-fetch|okhttp|java/", re.I)


def is_bot(user_agent: str) -> bool:
    return not user_agent or bool(BOT.search(user_agent))


def day_windows(days: int, now: float | None = None) -> list[tuple[str, str]]:
    """UTC day boundaries, newest first, as ISO timestamps (the free plan limits one query to about a day)."""
    now = now if now is not None else time.time()
    midnight = now - (now % 86400)
    out = []
    for i in range(days):
        start, end = midnight - i * 86400, (midnight + 86400 - i * 86400) if i == 0 else midnight - (i - 1) * 86400
        out.append((time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(min(end, now)))))
    return out


def gql(query: str, variables: dict) -> list[dict]:
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    req = urllib.request.Request("https://api.cloudflare.com/client/v4/graphql", method="POST",
                                 data=json.dumps({"query": query, "variables": variables}).encode(),
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.read()[:200].decode('utf-8', 'replace')}") from None
    if body.get("errors"):
        raise RuntimeError("; ".join(e.get("message", "?") for e in body["errors"])[:300])
    zones = ((body.get("data") or {}).get("viewer") or {}).get("zones") or [{}]
    return zones[0].get("httpRequestsAdaptiveGroups") or []


Q = """query($zone: String!, $since: Time!, $until: Time!) {
  viewer { zones(filter: {zoneTag: $zone}) {
    httpRequestsAdaptiveGroups(limit: 10000, filter: {datetime_geq: $since, datetime_lt: $until, requestSource: "eyeball",
        clientRequestPath_like: "/tools%%"}) {
      count
      dimensions { %s }
    } } } }"""


DIMENSIONS = ["clientRequestPath", "userAgent", "cacheStatus", "clientCountryName", "clientRefererHost", "clientIP"]
UNAVAILABLE: list[str] = []      # fields this Cloudflare plan will not give us; reported at the end


def fetch(since: str, until: str) -> list[dict]:
    """Ask for every dimension; when the plan refuses one, drop it and ask again."""
    while True:
        dims = [d for d in DIMENSIONS if d not in UNAVAILABLE]
        try:
            return gql(Q % " ".join(dims), {"zone": ZONE, "since": since, "until": until})
        except RuntimeError as exc:
            m = re.search(r"access to the field '([A-Za-z]+)'", str(exc))
            name = next((d for d in dims if m and d.lower() == m.group(1).lower() and d != "clientRequestPath"), None)
            if not name:
                raise
            UNAVAILABLE.append(name)


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    per_day: dict[str, dict[str, int]] = {}
    visitors: dict[str, set] = {p: set() for p in PAGES}
    referrers: dict[str, int] = {}
    countries: dict[str, int] = {}
    bots = humans = 0
    cached = uncached = 0
    last_error = ""
    reached = 0
    for since, until in day_windows(days):
        day = since[:10]
        try:
            rows = fetch(since, until)
        except RuntimeError as exc:
            last_error = str(exc)
            continue
        reached += 1
        for r in rows:
            d, n = r["dimensions"], r["count"]
            path = d["clientRequestPath"].rstrip("/") or "/"
            if path not in PAGES:
                continue
            if is_bot(d.get("userAgent", "")):
                bots += n
                continue
            humans += n
            per_day.setdefault(day, {}).setdefault(path, 0)
            per_day[day][path] += n
            visitors[path].add((day, d.get("clientIP")))
            if d.get("cacheStatus") in ("hit", "revalidated", "updating", "stale"):
                cached += n
            else:
                uncached += n
            ref = d.get("clientRefererHost") or "(direct or hidden)"
            referrers[ref] = referrers.get(ref, 0) + n
            c = d.get("clientCountryName") or "?"
            countries[c] = countries.get(c, 0) + n
    if not reached:
        print(f"Could not read analytics: {last_error}")
        sys.exit(1)
    print(f"Free tools, last {days} days, real visitors only ({humans} page loads; {bots} bot or crawler loads left out)")
    print("\nPage loads per day (visitor-days in brackets):")
    header = "day".ljust(12) + "".join(PAGES[p].ljust(24) for p in PAGES)
    print(header)
    for day in sorted(per_day, reverse=True):
        cells = ""
        for p in PAGES:
            n = per_day[day].get(p, 0)
            v = len({ip for d_, ip in visitors[p] if d_ == day})
            cells += f"{n} ({v})".ljust(24)
        print(day.ljust(12) + cells)
    print("\nTotals:")
    for p, label in PAGES.items():
        total = sum(per_day[d].get(p, 0) for d in per_day)
        print(f"  {label:<20} {total:>6} page loads, about {len({ip for _, ip in visitors[p]}):>5} distinct visitors")
    if "clientRefererHost" not in UNAVAILABLE:
        print("\nWhere visitors came from:")
        for ref, n in sorted(referrers.items(), key=lambda kv: -kv[1])[:12]:
            print(f"  {n:>6}  {ref}")
    if "clientCountryName" not in UNAVAILABLE:
        print("\nCountries:")
        for c, n in sorted(countries.items(), key=lambda kv: -kv[1])[:10]:
            print(f"  {n:>6}  {c}")
    if UNAVAILABLE:
        print("\nNot available on this Cloudflare plan: " + ", ".join(UNAVAILABLE))
    if cached + uncached:
        print(f"\nServed from Cloudflare's cache: {cached} of {cached + uncached} loads")
    if reached < days:
        print(f"\n(Only {reached} of {days} days were readable: {last_error})")


if __name__ == "__main__":
    main()
