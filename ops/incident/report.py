#!/usr/bin/env python3
"""Tell the owner what happened: one email (through the watchdog Worker, which holds the
email binding) and one GitHub issue as the permanent record.

Runs at the end of every repair, whatever happened before it. If an earlier step
broke, that is itself reported, so "the repair crashed" never looks like silence.

Environment: INCIDENT_ID, INCIDENT_KIND, FIRSTAID, PLAN, RESULT, HEALTHY_NOW (all JSON/strings
from earlier jobs), NEEDS (JSON of earlier job results), REPORT_SECRET, GH_TOKEN, RUN_URL, DRILL_APP.
Usage: python report.py <decision.json>
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as c  # noqa: E402
import plan  # noqa: E402

OWNER = "yasaminfayyaz"


def load(name: str) -> dict:
    try:
        value = json.loads(os.environ.get(name, "") or "{}")
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def md_safe(text: str) -> str:
    """Log-derived text goes into a code fence, with nothing that could close it or ping someone."""
    return text.replace("`", "'").replace("@", "@" + chr(0x200B))


def post_worker(payload: dict) -> bool:
    secret = os.environ.get("REPORT_SECRET", "")
    if not secret:
        c.log("warning: no report secret; the email was not sent")
        return False
    status, _, body = c.http(c.STATUS + "/api/report", method="POST", timeout=30,
                             headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json"},
                             body=json.dumps(payload).encode())
    c.log(f"worker report -> {status}")
    return status == 200


def issue_text(report: dict, run_url: str, private: bool) -> str:
    """The repository is public, so the issue carries only fixed wording from our own scripts.
    The AI's diagnosis (written from log text) goes in the email, and here only as a fallback
    when the email could not be sent."""
    lines = report["details"] if private else report["public_details"]
    body = [f"**{report['headline']}**", ""]
    if lines:
        body += ["```text", *[md_safe(d) for d in lines], "```", ""]
    if not private:
        body.append("The full diagnosis was sent by email.")
    if run_url:
        body.append(f"Repair run: {run_url}")
    if not report["resolved"]:
        body += ["", f"@{OWNER} this needs you."]
    return "\n".join(body)


def record_issue(incident_id: str, report: dict, run_url: str) -> tuple[str, int]:
    title_prefix = f"Incident {incident_id}"
    text = issue_text(report, run_url, private=False)
    try:
        try:
            c.gh_api("POST", f"repos/{c.REPO}/labels", {"name": "incident", "color": "d73a4a"})
        except RuntimeError:
            pass  # label already exists
        open_issues = c.gh_api("GET", f"repos/{c.REPO}/issues?labels=incident&state=open&per_page=30") or []
        existing = next((i for i in open_issues if i["title"].startswith(title_prefix)), None)
        if existing:
            number = existing["number"]
            c.gh_api("POST", f"repos/{c.REPO}/issues/{number}/comments", {"body": text})
        else:
            made = c.gh_api("POST", f"repos/{c.REPO}/issues", {
                "title": f"{title_prefix}: {report['headline'][:80]}", "body": text, "labels[]": "incident"})
            number = made["number"]
        if report["resolved"]:
            c.gh_api("PATCH", f"repos/{c.REPO}/issues/{number}", {"state": "closed"})
        return f"https://github.com/{c.REPO}/issues/{number}", number
    except RuntimeError as exc:
        c.log(f"warning: could not record the issue: {plan.redact(str(exc), 160)}")
        return "", 0


def main() -> None:
    incident_id = os.environ.get("INCIDENT_ID", "unknown")
    run_url = os.environ.get("RUN_URL", "")
    path = sys.argv[1] if len(sys.argv) > 1 else ""
    decision = None
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            decision = plan.parse_decision(fh.read())

    needs = load("NEEDS")
    broken = [f"{job} ({info.get('result')})" for job, info in needs.items()
              if isinstance(info, dict) and info.get("result") in ("failure", "cancelled")]

    healthy_env = os.environ.get("HEALTHY_NOW", "")
    if healthy_env in ("true", "false"):
        healthy = healthy_env == "true"
    else:
        view = c.site_view()
        healthy = bool(view.get("known") and view.get("level") == "ok" and (view.get("age_seconds") or 9999) < 150)

    ctx = {"incident_id": incident_id, "kind": os.environ.get("INCIDENT_KIND", ""), "healthy_now": healthy,
           "armed": c.armed(), "drill": c.is_drill(), "firstaid": load("FIRSTAID"), "decision": decision,
           "plan": load("PLAN"), "result": load("RESULT")}
    report = plan.compose_report(ctx)
    if broken:
        report["details"].append("The repair workflow itself had a problem in: " + ", ".join(broken))
        if report["resolved"] is False and report["headline"].startswith("Needs you: this is not something"):
            report["headline"] = "Needs you: the automatic repair could not finish"

    issue_url, issue_number = record_issue(incident_id, report, run_url)
    links = [u for u in (issue_url, run_url) if u]
    payload = {"incident_id": incident_id, "stage": report["stage"], "headline": report["headline"],
               "details": report["details"], "links": links, "resolved": report["resolved"],
               "summary": report["summary"], "public": report["public"]}
    if not post_worker(payload):
        c.log("warning: the email could not be sent through the watchdog; putting the full report on the issue instead")
        if issue_number:
            try:
                c.gh_api("POST", f"repos/{c.REPO}/issues/{issue_number}/comments",
                         {"body": "The email could not be sent, so here is the full report.\n\n" + issue_text(report, run_url, private=True)})
            except RuntimeError:
                pass
    c.log(f"reported: {report['stage']}: {report['headline']}")


if __name__ == "__main__":
    main()
