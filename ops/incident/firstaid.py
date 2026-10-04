#!/usr/bin/env python3
"""First aid: the cheap, safe, deterministic repair that needs no AI.

Most outages of this kind are a stuck or crashed process, and a restart fixes
them in about a minute. So this runs first, passes through the SAME gate as every
other action (plan.plan_action: kill switch, hourly limits, no restart during a
deploy), and only then restarts the live revision and waits for it to answer.

Inputs (environment): INCIDENT_ID, INCIDENT_KIND, DRILL_APP, AUTOFIX_ENABLED, GH_TOKEN
Outputs: firstaid (JSON: recovered, restarted, summary)
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as c  # noqa: E402
import plan  # noqa: E402


def done(recovered: bool, restarted: bool, summary: str) -> None:
    c.set_output("firstaid", {"recovered": recovered, "restarted": restarted, "summary": plan.clip(summary, 300)})


def main() -> None:
    kind = c.effective_kind()
    app = c.target_app()
    base = c.app_base(app)
    now = time.time()
    c.log(f"first aid for {app} (kind {kind}, armed {c.armed()})")

    first = c._probe_once(base)
    if first["healthy"]:
        if kind == "edge":
            view = c.site_view()
            ok = view.get("known") and view.get("level") == "ok" and (view.get("age_seconds") or 9999) < 180
            done(bool(ok), False, "The app is healthy; the problem is between Cloudflare and the app."
                 if not ok else "Customers can reach the site again.")
        else:
            done(True, False, "The app was already answering again when the repair started.")
        return
    if kind == "edge":
        done(False, False, "The app itself is also failing: " + ", ".join(first["failing"] or ["no answer"]))
        return

    revs = c.revisions(app)
    facts = {"armed": c.armed(), "kind": kind, "now": now,
             "ledger": c.ledger_read(app), "deploy_running": plan.deploy_in_flight(revs, now) and not c.is_drill(),
             "firstaid_restarted": False}
    gate = plan.plan_action({"action": "restart", "category": "process_hang", "confidence": "high", "diagnosis": "",
                             "customer_impact": "", "waf_rule_id": "", "suggested_followup": ""}, facts)
    if gate["mode"] != "run":
        done(False, False, ("Would have restarted the app: " if gate["mode"] == "dry_run" else "Did not restart: ") + gate["why"])
        return

    active = [r["name"] for r in revs if r["active"]]
    if not active:
        done(False, False, "No active revision to restart.")
        return
    started = time.time()
    try:
        for name in active:
            c.sh(["az", "containerapp", "revision", "restart", "-n", app, "-g", c.RG, "--revision", name, "--only-show-errors"], timeout=180)
    except RuntimeError as exc:
        c.ledger_add({"action": "restart", "incident": os.environ.get("INCIDENT_ID", ""), "ok": False})
        done(False, False, "The restart command failed: " + plan.redact(str(exc), 160))
        return
    c.ledger_add({"action": "restart", "incident": os.environ.get("INCIDENT_ID", ""), "ok": True})
    after = c.probe_origin(base, patience=240)
    took = int(time.time() - started)
    if after["healthy"]:
        done(True, True, f"Restarted the app; it was answering again {took} seconds later.")
    else:
        done(False, True, f"Restarted the app but it is still failing after {took} seconds: " + ", ".join(after["failing"] or ["no answer"]))


if __name__ == "__main__":
    main()
