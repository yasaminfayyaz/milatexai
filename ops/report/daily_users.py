#!/usr/bin/env python3
"""Daily user table, emailed to the owner through the watchdog Worker.

Runs in the "daily-report" GitHub environment, started every day at 12:00 UTC by the
watchdog (Cloudflare cron, so it is on time; GitHub's own scheduler drifts by hours).
It signs in to Azure with a read-only identity (Storage Table Data Reader, nothing else),
reads the users, projects and usage tables, compares with the snapshot of the previous
report (kept by the Worker, in private storage), and posts the email.

This repository is public and its run logs are public, so the log shows counts only.
Names, addresses and the table itself leave only through the email.

Environment: AZURE_STORAGE_ACCOUNT, DIGEST_SECRET. For a local test, set
AZURE_STORAGE_CONNECTION_STRING instead of signing in, and DRY_RUN=1 to print nothing but counts.
"""

from __future__ import annotations

import html
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

STATUS = "https://status.milatexai.com"


# --- pure logic (unit tested) ---------------------------------------------------------------

def build_rows(users: list[dict], projects: list[dict], usage: list[dict], month: str) -> list[dict]:
    """One row per user, from the raw table entities."""
    per_projects: dict[str, int] = {}
    for p in projects:
        per_projects[p.get("PartitionKey", "")] = per_projects.get(p.get("PartitionKey", ""), 0) + 1
    total: dict[str, int] = {}
    this_month: dict[str, int] = {}
    for u in usage:
        uid, n = u.get("PartitionKey", ""), int(u.get("count", 0) or 0)
        total[uid] = total.get(uid, 0) + n
        if u.get("RowKey") == month:
            this_month[uid] = this_month.get(uid, 0) + n
    rows = []
    for u in users:
        uid = u.get("RowKey", "")
        rows.append({"email": u.get("email") or "(none)", "plan": u.get("plan") or "free",
                     "admin": bool(u.get("is_admin")), "projects": per_projects.get(uid, 0),
                     "commits": total.get(uid, 0), "month": this_month.get(uid, 0),
                     "past_due_since": float(u.get("past_due_since") or 0), "notices": int(u.get("dunning_stage") or 0)})
    rows.sort(key=lambda r: (r["plan"] != "pro", -r["commits"], r["email"]))
    return rows


SOURCE_LABELS = {
    "claude_directory": "Claude's connector directory", "search": "Google or Bing", "reddit": "Reddit", "chatgpt": "ChatGPT",
    "social": "X, LinkedIn, Bluesky or YouTube", "friend": "A colleague or friend", "other": "Somewhere else",
}


def source_counts(usage: list[dict], month: str) -> list[dict]:
    """Answers to "How did you find MiLatexAI?" on the first connect form. They are stored as a
    count per answer and month under a pseudo-user "signup-source:<answer>", never tied to a person."""
    out: dict[str, dict] = {}
    for u in usage:
        pk = u.get("PartitionKey", "")
        if not pk.startswith("signup-source:"):
            continue
        key = pk.split(":", 1)[1]
        row = out.setdefault(key, {"source": SOURCE_LABELS.get(key, key), "month": 0, "total": 0})
        n = int(u.get("count", 0) or 0)
        row["total"] += n
        if u.get("RowKey") == month:
            row["month"] += n
    return sorted(out.values(), key=lambda r: (-r["month"], -r["total"], r["source"]))


TOOL_GROUPS = {
    "reads": {"list_projects", "list_files", "read_file", "get_sections", "read_section", "get_history",
              "list_checkpoints", "project_diff", "check_citations", "project_stats", "list_figures", "download_file"},
    "renders": {"check_compile", "show_table", "show_figure", "show_page", "tracked_changes_pdf", "arxiv_export"},
    "edits": {"checkpoint", "restore_file", "add_citation", "commit_figure", "commit_tikz", "edit_file",
              "write_file", "delete_file", "upload_file", "copy_file"},
}


def group_of(tool: str) -> str:
    for name, tools in TOOL_GROUPS.items():
        if tool in tools:
            return name
    return "other"      # account and project setup, billing, anything new


def activity_by_user(users: list[dict], activity: list[dict], day: str, days: int = 7) -> dict:
    """How accounts used the tools (leafbridge/activity.py keeps one row per copy of the server,
    user, day and tool; they are added up here). ``day`` is the last full UTC day."""
    from datetime import date, timedelta

    first = (date.fromisoformat(day) - timedelta(days=days - 1)).isoformat()
    emails = {u.get("RowKey", ""): (u.get("email") or "(none)", u.get("plan") or "free", bool(u.get("is_admin"))) for u in users}
    per: dict[str, dict] = {}
    for r in activity:
        uid = r.get("PartitionKey", "")
        parts = str(r.get("RowKey", "")).split("|")
        if len(parts) < 2 or not (first <= parts[0] <= day):
            continue
        d, tool = parts[0], parts[1]
        calls, errors = int(r.get("calls", 0) or 0), int(r.get("errors", 0) or 0)
        u = per.setdefault(uid, {"days": set(), "yesterday": {"calls": 0, "errors": 0, "reads": 0, "renders": 0, "edits": 0, "other": 0},
                                 "tools": {}})
        u["days"].add(d)
        if d == day:
            y = u["yesterday"]
            y["calls"] += calls
            y["errors"] += errors
            y[group_of(tool)] += calls
            u["tools"][tool] = u["tools"].get(tool, 0) + calls
    out = []
    for uid, u in per.items():
        if not u["yesterday"]["calls"]:
            continue
        email, plan, admin = emails.get(uid, ("(unknown)", "free", False))
        top = max(u["tools"].items(), key=lambda kv: (kv[1], kv[0]))
        out.append({"email": email, "plan": plan, "admin": admin, **u["yesterday"], "days_active": len(u["days"]),
                    "top": f"{top[0]} ({top[1]})"})
    out.sort(key=lambda r: (-r["calls"], r["email"]))
    free = [r for r in out if r["plan"] != "pro" and not r["admin"]]
    pro = [r for r in out if r["plan"] == "pro"]
    free_total = sum(1 for u in users if (u.get("plan") or "free") != "pro" and not u.get("is_admin"))
    return {"day": day, "free": free, "free_quiet": max(free_total - len(free), 0), "pro_users": len(pro),
            "pro_calls": sum(r["calls"] for r in pro)}


def activity_lines(act: dict | None) -> list[str]:
    if not act:
        return []
    free = act["free"]
    lines = ["", f"Free users' tool use on {act['day']} (UTC). Reads and renders are free; only edits count toward the 10 commits:"]
    if not free:
        lines.append("  No free user made a tool call.")
    else:
        lines.append(f"  {'email':<34} calls reads render edits other errors days7  most used")
        for r in free:
            lines.append(f"  {r['email']:<34} {r['calls']:<5} {r['reads']:<5} {r['renders']:<6} {r['edits']:<5} {r['other']:<5} "
                         f"{r['errors']:<6} {r['days_active']:<5}  {r['top']}")
    lines.append(f"  {len(free)} free users active, {act['free_quiet']} made no calls. "
                 f"Pro: {act['pro_users']} active, {act['pro_calls']} calls.")
    return lines


def _activity_html(act: dict | None) -> str:
    if not act:
        return ""
    e = html.escape
    cell = "padding:4px 8px;border-bottom:1px solid #e5e7eb;"
    heads = ("Email", "Calls", "Reads", "Renders", "Edits", "Other", "Errors", "Days (7d)", "Most used")
    head = "".join(f'<th style="{cell}text-align:{"left" if i in (0, 8) else "right"};background:#f3f4f6">{h}</th>'
                   for i, h in enumerate(heads))
    body = "".join(
        f'<tr><td style="{cell}">{e(r["email"])}</td>'
        + "".join(f'<td style="{cell}text-align:right">{r[k]}</td>' for k in ("calls", "reads", "renders", "edits", "other", "errors", "days_active"))
        + f'<td style="{cell}">{e(r["top"])}</td></tr>' for r in act["free"])
    table = (f'<table style="border-collapse:collapse"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'
             if act["free"] else '<p style="margin:0">No free user made a tool call.</p>')
    return (f'<p style="margin:18px 0 4px"><strong>Free users tool use on {e(act["day"])} (UTC)</strong> '
            '<span style="color:#6b7280">(reads and renders are free; only edits count toward the 10 commits)</span></p>'
            + table
            + f'<p style="margin:6px 0 0;color:#6b7280">{len(act["free"])} free users active, {act["free_quiet"]} made no calls. '
              f'Pro: {act["pro_users"]} active, {act["pro_calls"]} calls.</p>')


GRACE_DAYS = 7          # keep in step with leafbridge/dunning.py
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December")


def _day(ts: float) -> str:
    t = time.gmtime(ts)
    return f"{_MONTHS[t.tm_mon - 1]} {t.tm_mday}"


def past_due_lines(rows: list[dict]) -> list[str]:
    """Pro accounts whose renewal payment is unpaid: since when, how many reminders went out, when Pro ends."""
    out = []
    for r in rows:
        if r.get("past_due_since") and r["plan"] == "pro":
            end = r["past_due_since"] + GRACE_DAYS * 86400
            out.append(f"{r['email']}: unpaid since {_day(r['past_due_since'])}, {r['notices']} of 2 emails sent, "
                       f"Pro ends {_day(end)} if still unpaid")
    return out


def snapshot_of(rows: list[dict]) -> dict:
    return {r["email"]: {"commits": r["commits"], "projects": r["projects"], "plan": r["plan"]} for r in rows}


REVIEWER_EMAIL = "reviewer@milatexai.com"   # the connector directory's review account, never counted as a Pro customer


def compare(rows: list[dict], previous: dict) -> dict:
    """Annotate rows with what changed since the previous report and summarise."""
    first = not previous
    new_users, upgraded = [], []
    for r in rows:
        before = previous.get(r["email"])
        if first:
            r["delta"], r["note"] = None, ""
            continue
        if before is None:
            r["delta"], r["note"] = r["commits"], "new"
            new_users.append(r["email"])
            continue
        r["delta"] = r["commits"] - int(before.get("commits", 0))
        notes = []
        if r["projects"] != before.get("projects", r["projects"]):
            diff = r["projects"] - before.get("projects", 0)
            notes.append(f"{diff:+d} project" + ("" if abs(diff) == 1 else "s"))
        if r["plan"] != before.get("plan", r["plan"]):
            notes.append(f"plan {before.get('plan')} to {r['plan']}")
            if r["plan"] == "pro":
                upgraded.append(r["email"])
        r["note"] = ", ".join(notes)
    # "Project users": only people who have connected a project count in the title numbers.
    proj = [r for r in rows if r["projects"] > 0]
    new_project = [] if first else [
        r["email"] for r in proj if not int((previous.get(r["email"]) or {}).get("projects", 0) or 0)]
    return {
        "first": first,
        "project_users": len(proj),
        "project_pro": sum(1 for r in proj if r["plan"] == "pro" and r["email"] != REVIEWER_EMAIL),
        "project_new": len(new_project),
        "project_commit_delta": None if first else sum(r["delta"] or 0 for r in proj),
        "users": len(rows),
        "pro": sum(1 for r in rows if r["plan"] == "pro"),
        "with_project": sum(1 for r in rows if r["projects"] > 0),
        "commits": sum(r["commits"] for r in rows),
        "commit_delta": None if first else sum(r["delta"] or 0 for r in rows),
        "new_users": new_users,
        "upgraded": upgraded,
    }


def subject_of(summary: dict) -> str:
    """The email title counts only users who have connected a project ("project users")."""
    head = f"Project users: {summary['project_users']} total, {summary['project_pro']} pro"
    if summary["first"]:
        return head + " (first report)"
    return f"{head}, {summary['project_new']} new, {summary['project_commit_delta']:+d} commits"


def _delta(r: dict) -> str:
    if r["delta"] is None:
        return ""
    return f"{r['delta']:+d}" if r["delta"] else "0"


def service_line(service: dict | None) -> str:
    """One line about the service itself: is the free tier open, and how many copies ran."""
    if not service:
        return ""
    parts = []
    if service.get("free_open") is True:
        parts.append("free tier open")
    elif service.get("free_open") is False:
        parts.append("free tier PAUSED (spending limit reached; Pro unaffected)")
    if service.get("peak_copies") is not None:
        parts.append(f"most copies running in the last 24 h: {service['peak_copies']} of {service.get('max_copies', 5)}")
    return ("Service: " + ", ".join(parts)) if parts else ""


def render_text(rows: list[dict], summary: dict, sources: list[dict] | None = None, service: dict | None = None,
                activity: dict | None = None) -> str:
    lines = [subject_of(summary), ""]
    lines.append(f"{summary['users']} users | {summary['pro']} pro | {summary['with_project']} with a project | "
                 f"{summary['commits']} commits all-time")
    if service_line(service):
        lines.append(service_line(service))
    for pd in past_due_lines(rows):
        lines.append("Payment problem: " + pd)
    if summary["first"]:
        lines.append("First report, so there is nothing to compare with yet.")
    else:
        if summary["new_users"]:
            lines.append("New since yesterday: " + ", ".join(summary["new_users"]))
        if summary["upgraded"]:
            lines.append("Upgraded to pro: " + ", ".join(summary["upgraded"]))
    lines.append("")
    width = max([len(r["email"]) for r in rows] + [5])
    lines.append(f"{'email'.ljust(width)}  plan  proj  commits  change")
    for r in rows:
        mark = " (admin)" if r["admin"] else ""
        lines.append(f"{(r['email'] + mark).ljust(width + 8)} {r['plan']:<5} {r['projects']:<5} {r['commits']:<8} "
                     f"{(_delta(r) + ' ' + r['note']).strip()}")
    if sources:
        lines += ["", "How people said they found us (asked once, on the first connect form):"]
        lines += [f"  {r['source']}: {r['month']} this month, {r['total']} in total" for r in sources]
    lines += activity_lines(activity)
    return "\n".join(lines) + "\n"


def render_html(rows: list[dict], summary: dict, sources: list[dict] | None = None, service: dict | None = None,
                activity: dict | None = None) -> str:
    e = html.escape
    cell = "padding:6px 10px;border-bottom:1px solid #e5e7eb;"
    head = "".join(f'<th style="{cell}text-align:{a};background:#f3f4f6">{t}</th>'
                   for t, a in (("Email", "left"), ("Plan", "left"), ("Projects", "right"), ("Commits", "right"), ("Change", "right")))
    body = []
    for r in rows:
        changed = bool(r["delta"]) or bool(r["note"])
        weight = "font-weight:600;" if changed else ""
        who = e(r["email"]) + (' <span style="color:#6b7280">(admin)</span>' if r["admin"] else "")
        change = e((_delta(r) + (" " + r["note"] if r["note"] else "")).strip())
        body.append(
            f'<tr style="{weight}"><td style="{cell}">{who}</td><td style="{cell}">{e(r["plan"])}</td>'
            f'<td style="{cell}text-align:right">{r["projects"]}</td><td style="{cell}text-align:right">{r["commits"]}</td>'
            f'<td style="{cell}text-align:right">{change}</td></tr>')
    notes = []
    if summary["first"]:
        notes.append("First report, so there is nothing to compare with yet.")
    if summary["new_users"]:
        notes.append("New since yesterday: " + e(", ".join(summary["new_users"])))
    if summary["upgraded"]:
        notes.append("Upgraded to pro: " + e(", ".join(summary["upgraded"])))
    if service_line(service):
        notes.insert(0, e(service_line(service)))
    notes[:0] = ["<strong>Payment problem:</strong> " + e(pd) for pd in past_due_lines(rows)]
    return (
        '<div style="font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#111827">'
        f'<p style="margin:0 0 4px"><strong>{summary["users"]}</strong> users, <strong>{summary["pro"]}</strong> pro, '
        f'<strong>{summary["with_project"]}</strong> with a project, <strong>{summary["commits"]}</strong> commits all-time'
        + ("" if summary["first"] else f' (<strong>{summary["commit_delta"]:+d}</strong> since yesterday)') + '</p>'
        + "".join(f'<p style="margin:0 0 4px">{n}</p>' for n in notes)
        + f'<table style="border-collapse:collapse;margin-top:12px"><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>'
        + _sources_html(sources) + _activity_html(activity) + '</div>')


def _sources_html(sources: list[dict] | None) -> str:
    if not sources:
        return ""
    cell = "padding:4px 10px;border-bottom:1px solid #e5e7eb;"
    rows = "".join(f'<tr><td style="{cell}">{html.escape(r["source"])}</td><td style="{cell}text-align:right">{r["month"]}</td>'
                   f'<td style="{cell}text-align:right">{r["total"]}</td></tr>' for r in sources)
    return ('<p style="margin:18px 0 4px"><strong>How people said they found us</strong> '
            '<span style="color:#6b7280">(asked once, on the first connect form)</span></p>'
            '<table style="border-collapse:collapse"><thead><tr>'
            f'<th style="{cell}text-align:left;background:#f3f4f6">Answer</th><th style="{cell}text-align:right;background:#f3f4f6">This month</th>'
            f'<th style="{cell}text-align:right;background:#f3f4f6">All time</th></tr></thead><tbody>{rows}</tbody></table>')


# --- I/O ------------------------------------------------------------------------------------------

def entities(account: str, table: str, flt: str | None = None) -> list[dict]:
    az = shutil.which("az") or "az"
    out: list[dict] = []
    marker: dict | None = None
    while True:
        cmd = [az, "storage", "entity", "query", "--table-name", table, "--account-name", account, "-o", "json", "--only-show-errors"]
        if flt:
            cmd += ["--filter", flt]
        if os.environ.get("AZURE_STORAGE_CONNECTION_STRING"):
            cmd += ["--connection-string", os.environ["AZURE_STORAGE_CONNECTION_STRING"]]
        else:
            cmd += ["--auth-mode", "login"]
        if marker:
            cmd += ["--marker", *[f"{k}={v}" for k, v in marker.items()]]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=180)
        if res.returncode != 0:
            raise RuntimeError(f"reading table {table} failed: {res.stderr.strip()[:200]}")
        page = json.loads(res.stdout)
        out.extend(page.get("items", []))
        marker = page.get("nextMarker")
        if not marker:
            return out


def worker(method: str, path: str, body: dict | None = None) -> dict:
    secret = os.environ.get("DIGEST_SECRET", "")
    if not secret:
        raise RuntimeError("DIGEST_SECRET is not set")
    req = urllib.request.Request(STATUS + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json",
                                          "User-Agent": "milatexai-daily-report/1.0"})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise RuntimeError(f"the watchdog refused the request (HTTP {exc.code})") from None
            last = f"HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001
            last = type(exc).__name__
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"the watchdog did not answer ({last})")


APP_ID = ("/subscriptions/f1b1a285-7029-47fc-88a7-b83cf5b8c938/resourceGroups/milatexai-rg"
          "/providers/Microsoft.App/containerApps/milatexai-app")
MAX_COPIES = 5   # keep in step with MAX_REPLICAS in ops/deploy.sh


def service_status() -> dict:
    """Free tier open or paused (public health endpoint) and the most copies that ran in the
    last 24 hours (Azure metric). Each part is optional: a failure just leaves it out."""
    out: dict = {"max_copies": MAX_COPIES}
    try:
        req = urllib.request.Request("https://milatexai.com/health/capacity",
                                     headers={"User-Agent": "milatexai-daily-report/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            out["free_open"] = bool(json.loads(resp.read()).get("free_open"))
    except Exception:  # noqa: BLE001
        pass
    try:
        az = shutil.which("az") or "az"
        start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400))
        res = subprocess.run([az, "monitor", "metrics", "list", "--resource", APP_ID, "--metric", "Replicas",
                              "--aggregation", "Maximum", "--interval", "PT1H", "--start-time", start,
                              "-o", "json", "--only-show-errors"], capture_output=True, text=True, timeout=120)
        if res.returncode == 0:
            series = json.loads(res.stdout)["value"][0]["timeseries"]
            peaks = [p.get("maximum") for t in series for p in t.get("data", []) if p.get("maximum") is not None]
            if peaks:
                out["peak_copies"] = int(max(peaks))
    except Exception:  # noqa: BLE001
        pass
    return out


def tool_activity(account: str, users: list[dict]) -> dict | None:
    """Yesterday's tool use per account, or None if the table does not exist yet or cannot be read."""
    day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))
    first = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 7 * 86400))
    try:
        return activity_by_user(users, entities(account, "activity", f"RowKey ge '{first}'"), day)
    except RuntimeError as exc:
        print(f"tool activity unavailable: {str(exc)[:120]}")
        return None


def main() -> None:
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    month = time.strftime("%Y-%m", time.gmtime())
    usage = entities(account, "usage")
    users = entities(account, "users")
    rows = build_rows(users, entities(account, "projects"), usage, month)
    sources = source_counts(usage, month)
    dry = os.environ.get("DRY_RUN") == "1"
    previous = {} if dry else worker("GET", "/api/digest-state")
    summary = compare(rows, previous)
    print(f"{summary['users']} users, {summary['pro']} pro, {summary['commits']} commits"
          + ("" if summary["first"] else f", {len(summary['new_users'])} new, {summary['commit_delta']:+d} commits since the last report"))
    if dry:
        return
    service = service_status()
    print("service:", service_line(service) or "unavailable")
    act = tool_activity(account, users)
    if act:
        print(f"tool use: {len(act['free'])} free users active, {act['free_quiet']} quiet")
    worker("POST", "/api/digest", {"subject": subject_of(summary), "text": render_text(rows, summary, sources, service, act),
                                   "html": render_html(rows, summary, sources, service, act), "snapshot": snapshot_of(rows)})
    print("emailed")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
