#!/usr/bin/env python3
"""Carry out the AI's proposal, but only through plan.plan_action.

This script holds the cloud credentials; the AI that proposed the action does not.
The proposal is parsed and clamped (plan.parse_decision), then judged against the
menu, the preconditions, the hourly and daily limits and the kill switch. Only an
action that comes back mode="run" is performed, and its target (which version to
roll back to, which firewall rule) comes from facts this script reads itself.

Usage: python execute.py <decision.json>
Outputs: plan (JSON), result (JSON), healthy_now (true/false)
"""

from __future__ import annotations

import calendar
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as c  # noqa: E402
import plan  # noqa: E402


def restart(app: str, base: str) -> dict:
    names = [r["name"] for r in c.revisions(app) if r["active"]]
    if not names:
        return {"ran": True, "ok": False, "detail": "There was no active revision to restart."}
    for name in names:
        c.sh(["az", "containerapp", "revision", "restart", "-n", app, "-g", c.RG, "--revision", name, "--only-show-errors"], timeout=180)
    return {"ran": True, "ok": True, "detail": "Restarted " + ", ".join(names) + "."}


def rollback(app: str, sha: str) -> dict:
    """Redeploy the previous version through the normal deploy workflow, which has its own
    smoke tests, its own automatic rollback, and a lock so two deploys never overlap."""
    started = time.time()
    cmd = ["gh", "workflow", "run", "deploy.yml", "--repo", c.REPO, "--ref", "main", "-f", f"sha={sha}"]
    if c.is_drill():
        cmd += ["-f", f"app={app}"]
    out = c.sh(cmd)
    found = re.search(r"/actions/runs/(\d+)", out)          # gh prints the URL of the run it started
    run_id = int(found.group(1)) if found else None
    deadline = time.time() + 120
    while run_id is None and time.time() < deadline:         # older gh versions print nothing: look for it
        time.sleep(6)
        runs = c.gh_api("GET", f"repos/{c.REPO}/actions/workflows/deploy.yml/runs?event=workflow_dispatch&per_page=5") or {}
        for r in runs.get("workflow_runs", []):
            made = calendar.timegm(time.strptime(r["created_at"], "%Y-%m-%dT%H:%M:%SZ"))
            if made >= started - 5:
                run_id = r["id"]
                break
    if run_id is None:
        return {"ran": True, "ok": False, "detail": "The rollback deploy never started."}
    end = time.time() + 20 * 60
    while time.time() < end:
        run = c.gh_api("GET", f"repos/{c.REPO}/actions/runs/{run_id}") or {}
        if run.get("status") == "completed":
            good = run.get("conclusion") == "success"
            return {"ran": True, "ok": good, "detail": f"Redeployed {sha[:7]} ({'succeeded' if good else run.get('conclusion')})."}
        time.sleep(15)
    return {"ran": True, "ok": False, "detail": "The rollback deploy did not finish in 20 minutes."}


def purge_cache() -> dict:
    c.cf("POST", f"/zones/{c.CF_ZONE}/purge_cache", {"purge_everything": True})
    return {"ran": True, "ok": True, "detail": "Cleared the Cloudflare cache."}


def disable_rule(rule_id: str) -> dict:
    # Cloudflare wants the whole rule back on an update (a bare {"enabled": false} is
    # rejected), so take the stored rule and change only that one field.
    ruleset_id, raw = c.waf_rules_raw()
    rule = next((r for r in raw if r.get("id") == rule_id), None)
    if rule is None:
        return {"ran": True, "ok": False, "detail": "The firewall rule was not found when it came to switching it off."}
    keep = ("action", "action_parameters", "expression", "description", "logging", "ratelimit")
    body = {k: rule[k] for k in keep if k in rule}
    body["enabled"] = False
    c.cf("PATCH", f"/zones/{c.CF_ZONE}/rulesets/{ruleset_id}/rules/{rule_id}", body)
    name = rule.get("description") or rule_id
    return {"ran": True, "ok": True, "detail": f"Switched off the firewall rule '{name}'. It stays off until you switch it back on in Cloudflare."}


def wait_healthy(kind: str, base: str, patience: int) -> bool:
    """Is the service answering? Checks at least once, then keeps checking for `patience` seconds."""
    if kind == "edge":
        end = time.time() + patience
        while True:
            view = c.site_view()
            if view.get("known") and view.get("level") == "ok" and (view.get("age_seconds") or 9999) < 120:
                return True
            if time.time() >= end:
                return False
            time.sleep(15)
    return c.probe_origin(base, patience=patience)["healthy"]


def main() -> None:
    raw_path = sys.argv[1] if len(sys.argv) > 1 else ""
    raw = ""
    if raw_path and os.path.exists(raw_path):
        with open(raw_path, encoding="utf-8") as fh:
            raw = fh.read()
    decision = plan.parse_decision(raw) if raw.strip() else None

    kind = c.effective_kind()
    app = c.target_app()
    base = c.app_base(app)
    now = time.time()
    try:
        firstaid = json.loads(os.environ.get("FIRSTAID", "") or "{}")
    except ValueError:
        firstaid = {}

    show = c.az("containerapp", "show", "-n", app, "-g", c.RG)
    tags = plan.parse_deploy_tags(show.get("tags"))
    facts = {
        "armed": c.armed(), "kind": kind, "now": now,
        "ledger": c.ledger_read(app),
        "deploy_running": plan.deploy_in_flight(c.revisions(app), now) and not c.is_drill(),
        "firstaid_restarted": bool(firstaid.get("restarted")),
        "rollback_target": tags["previous"], "current_deployed_at": tags["deployed_at"], "waf_rules": [],
    }
    if decision and decision["action"] == "disable_waf_rule":
        try:
            facts["waf_rules"] = c.waf_rules()[1]
        except RuntimeError as exc:
            c.log(f"warning: could not read firewall rules: {plan.redact(str(exc), 160)}")

    verdict = plan.plan_action(decision, facts)
    c.log(f"decision: {decision and decision['action']} -> {verdict}")
    result = {"ran": False, "ok": None, "detail": ""}

    if verdict["mode"] == "run":
        action = verdict["do"]
        try:
            if action == "restart":
                result = restart(app, base)
            elif action == "rollback":
                result = rollback(app, verdict["target"])
            elif action == "purge_cache":
                result = purge_cache()
            elif action == "disable_waf_rule":
                result = disable_rule(verdict["target"])
        except RuntimeError as exc:
            result = {"ran": True, "ok": False, "detail": "The change failed: " + plan.redact(str(exc), 200)}
        healthy = wait_healthy(kind, base, 240 if action != "rollback" else 120) if result["ok"] else False
        c.ledger_add({"action": action, "incident": os.environ.get("INCIDENT_ID", ""), "ok": bool(result["ok"]), "healed": healthy})
        if result["ok"]:
            result["detail"] += " The service " + ("answered again afterwards." if healthy else "was still failing afterwards.")
    else:
        healthy = wait_healthy(kind, base, 0)

    c.set_output("plan", verdict)
    c.set_output("result", result)
    c.set_output("healthy_now", "true" if healthy else "false")


if __name__ == "__main__":
    main()
