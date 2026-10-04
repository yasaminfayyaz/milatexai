#!/usr/bin/env python3
"""Second line of defence: is the watchdog itself still alive?

The watchdog runs on Cloudflare every minute. This runs on GitHub's own scheduler
(slow, sometimes hours late, but a completely separate system) and asks the one question
the watchdog cannot answer about itself: "are you still checking?". If its last check is
too old, a GitHub issue that @mentions the owner is opened, which GitHub emails.

Usage: python backstop.py        Environment: GH_TOKEN
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as c  # noqa: E402

STALE_SECONDS = 15 * 60
OWNER = "yasaminfayyaz"


def judge(status: int, body: str, now: float) -> tuple[str, str]:
    """('ok' | 'stale' | 'unknown', plain reason). 'unknown' means we could not tell
    (for example Cloudflare turned this runner away), which must never raise an alarm."""
    if status in (401, 403, 429) or (status == 200 and not body.lstrip().startswith("{")):
        return "unknown", f"Cloudflare did not let this check through (HTTP {status})."
    if status != 200:
        return "stale", f"The watchdog's status page did not answer (HTTP {status})."
    try:
        age = json.loads(body).get("ageSeconds")
    except ValueError:
        return "stale", "The watchdog's status page returned something unreadable."
    if age is None:
        return "stale", "The watchdog has never completed a check."
    if age > STALE_SECONDS:
        return "stale", f"The watchdog's last check was {int(age / 60)} minutes ago."
    return "ok", f"The watchdog checked {int(age)} seconds ago."


def main() -> None:
    status, _, body = c.http(c.STATUS + "/api/heartbeat", timeout=20)
    for _ in range(2):                                  # confirm before raising an alarm
        verdict, why = judge(status, body, time.time())
        if verdict != "stale":
            break
        time.sleep(20)
        status, _, body = c.http(c.STATUS + "/api/heartbeat", timeout=20)
    c.log(f"{verdict}: {why}")

    site_status, _, _ = c.http(c.SITE + "/health/live", timeout=20)
    site = "answering" if site_status == 200 else f"not answering (HTTP {site_status})"

    issues = c.gh_api("GET", f"repos/{c.REPO}/issues?labels=watchdog-silent&state=open&per_page=1") or []
    if verdict == "stale":
        text = (f"{why}\n\nThe site itself is {site}.\n\nThe watchdog runs on Cloudflare "
                f"(https://status.milatexai.com). While it is silent, nothing is watching the site "
                f"or starting automatic repairs. Check the Worker in the Cloudflare dashboard "
                f"(Workers, milatexai-watchdog) and re-run the 'watchdog-deploy' workflow.")
        if not issues:
            try:
                c.gh_api("POST", f"repos/{c.REPO}/labels", {"name": "watchdog-silent", "color": "b60205"})
            except RuntimeError:
                pass
            c.gh_api("POST", f"repos/{c.REPO}/issues", {
                "title": "The watchdog has gone quiet", "labels[]": "watchdog-silent",
                "body": f"@{OWNER} {text}"})
            c.log("opened an issue")
        else:
            c.log("an issue is already open; not repeating it")
    elif verdict == "ok" and issues:
        number = issues[0]["number"]
        c.gh_api("POST", f"repos/{c.REPO}/issues/{number}/comments", {"body": f"The watchdog is checking again. {why}"})
        c.gh_api("PATCH", f"repos/{c.REPO}/issues/{number}", {"state": "closed"})
        c.log("closed the issue")


if __name__ == "__main__":
    main()
