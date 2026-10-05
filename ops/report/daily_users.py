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
                     "commits": total.get(uid, 0), "month": this_month.get(uid, 0)})
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


def snapshot_of(rows: list[dict]) -> dict:
    return {r["email"]: {"commits": r["commits"], "projects": r["projects"], "plan": r["plan"]} for r in rows}


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
    return {
        "first": first,
        "users": len(rows),
        "pro": sum(1 for r in rows if r["plan"] == "pro"),
        "with_project": sum(1 for r in rows if r["projects"] > 0),
        "commits": sum(r["commits"] for r in rows),
        "commit_delta": None if first else sum(r["delta"] or 0 for r in rows),
        "new_users": new_users,
        "upgraded": upgraded,
    }


def subject_of(summary: dict) -> str:
    if summary["first"]:
        return f"Daily users: {summary['users']} total, {summary['commits']} commits (first report)"
    return (f"Daily users: {summary['users']} total, {len(summary['new_users'])} new, "
            f"{summary['commit_delta']:+d} commits")


def _delta(r: dict) -> str:
    if r["delta"] is None:
        return ""
    return f"{r['delta']:+d}" if r["delta"] else "0"


def render_text(rows: list[dict], summary: dict, sources: list[dict] | None = None) -> str:
    lines = [subject_of(summary), ""]
    lines.append(f"{summary['users']} users | {summary['pro']} pro | {summary['with_project']} with a project | "
                 f"{summary['commits']} commits all-time")
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
    return "\n".join(lines) + "\n"


def render_html(rows: list[dict], summary: dict, sources: list[dict] | None = None) -> str:
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
    return (
        '<div style="font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#111827">'
        f'<p style="margin:0 0 4px"><strong>{summary["users"]}</strong> users, <strong>{summary["pro"]}</strong> pro, '
        f'<strong>{summary["with_project"]}</strong> with a project, <strong>{summary["commits"]}</strong> commits all-time'
        + ("" if summary["first"] else f' (<strong>{summary["commit_delta"]:+d}</strong> since yesterday)') + '</p>'
        + "".join(f'<p style="margin:0 0 4px">{n}</p>' for n in notes)
        + f'<table style="border-collapse:collapse;margin-top:12px"><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>'
        + _sources_html(sources) + '</div>')


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

def entities(account: str, table: str) -> list[dict]:
    az = shutil.which("az") or "az"
    out: list[dict] = []
    marker: dict | None = None
    while True:
        cmd = [az, "storage", "entity", "query", "--table-name", table, "--account-name", account, "-o", "json", "--only-show-errors"]
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


def main() -> None:
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    month = time.strftime("%Y-%m", time.gmtime())
    usage = entities(account, "usage")
    rows = build_rows(entities(account, "users"), entities(account, "projects"), usage, month)
    sources = source_counts(usage, month)
    dry = os.environ.get("DRY_RUN") == "1"
    previous = {} if dry else worker("GET", "/api/digest-state")
    summary = compare(rows, previous)
    print(f"{summary['users']} users, {summary['pro']} pro, {summary['commits']} commits"
          + ("" if summary["first"] else f", {len(summary['new_users'])} new, {summary['commit_delta']:+d} commits since the last report"))
    if dry:
        return
    worker("POST", "/api/digest", {"subject": subject_of(summary), "text": render_text(rows, summary, sources),
                                   "html": render_html(rows, summary, sources), "snapshot": snapshot_of(rows)})
    print("emailed")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
