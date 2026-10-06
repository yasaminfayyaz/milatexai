"""The daily user table (ops/report/daily_users.py): the numbers, the comparison with
yesterday, and that nothing in the HTML can be injected by a user's email address."""

from __future__ import annotations

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("daily_users", Path(__file__).resolve().parents[1] / "ops" / "report" / "daily_users.py")
du = importlib.util.module_from_spec(spec)
spec.loader.exec_module(du)

USERS = [
    {"RowKey": "u1", "email": "pro@example.com", "plan": "pro"},
    {"RowKey": "u2", "email": "free@example.com", "plan": "free"},
    {"RowKey": "u3", "email": "admin@example.com", "plan": "free", "is_admin": True},
    {"RowKey": "u4", "email": "", "plan": "free"},
]
PROJECTS = [{"PartitionKey": "u1"}, {"PartitionKey": "u1"}, {"PartitionKey": "u2"}]
USAGE = [
    {"PartitionKey": "u1", "RowKey": "2026-09", "count": 100}, {"PartitionKey": "u1", "RowKey": "2026-10", "count": 11},
    {"PartitionKey": "u2", "RowKey": "2026-10", "count": 4}, {"PartitionKey": "u3", "RowKey": "2026-09", "count": "7"},
]


def rows():
    return du.build_rows(USERS, PROJECTS, USAGE, "2026-10")


def test_rows_count_projects_and_commits():
    by = {r["email"]: r for r in rows()}
    assert by["pro@example.com"]["projects"] == 2 and by["pro@example.com"]["commits"] == 111 and by["pro@example.com"]["month"] == 11
    assert by["free@example.com"]["commits"] == 4
    assert by["admin@example.com"]["commits"] == 7 and by["admin@example.com"]["admin"] is True
    assert by["(none)"]["commits"] == 0 and by["(none)"]["projects"] == 0


def test_pro_first_then_most_active():
    assert [r["email"] for r in rows()] == ["pro@example.com", "admin@example.com", "free@example.com", "(none)"]


def test_first_report_has_nothing_to_compare():
    r = rows()
    s = du.compare(r, {})
    assert s["first"] is True and s["commit_delta"] is None and r[0]["delta"] is None
    assert "first report" in du.subject_of(s)


def test_changes_since_the_previous_report():
    previous = {"pro@example.com": {"commits": 105, "projects": 1, "plan": "pro"},
                "free@example.com": {"commits": 4, "projects": 1, "plan": "free"},
                "admin@example.com": {"commits": 7, "projects": 0, "plan": "free"}}
    r = rows()
    s = du.compare(r, previous)
    by = {x["email"]: x for x in r}
    assert by["pro@example.com"]["delta"] == 6 and "+1 project" in by["pro@example.com"]["note"]
    assert by["free@example.com"]["delta"] == 0
    assert by["(none)"]["note"] == "new"
    assert s["new_users"] == ["(none)"] and s["commit_delta"] == 6
    assert du.subject_of(s) == "Daily users: 4 total, 1 new, +6 commits"


def test_an_upgrade_is_called_out():
    previous = {"pro@example.com": {"commits": 111, "projects": 2, "plan": "free"}}
    s = du.compare(rows(), previous)
    assert s["upgraded"] == ["pro@example.com"]


def test_html_escapes_user_supplied_text():
    evil = [{"RowKey": "x", "email": '<script>alert(1)</script>"@evil.example', "plan": "<b>pro</b>"}]
    r = du.build_rows(evil, [], [], "2026-10")
    s = du.compare(r, {"someone@else.example": {"commits": 1, "projects": 0, "plan": "free"}})
    page = du.render_html(r, s)
    assert "<script>" not in page and "<b>pro</b>" not in page and "&lt;script&gt;" in page


def test_text_version_lists_everyone_and_marks_the_admin():
    r = rows()
    text = du.render_text(r, du.compare(r, {}))
    for email in ("pro@example.com", "free@example.com", "admin@example.com (admin)"):
        assert email in text
    assert chr(0x2014) not in text and chr(0x2014) not in du.render_html(r, du.compare(r, {}))


def test_snapshot_round_trips_into_the_next_comparison():
    r = rows()
    snap = du.snapshot_of(r)
    again = du.compare(rows(), snap)
    assert again["commit_delta"] == 0 and again["new_users"] == []


# --- traffic report helpers ------------------------------------------------------------------------

def _traffic():
    spec = importlib.util.spec_from_file_location("traffic", Path(__file__).resolve().parents[1] / "ops" / "report" / "traffic.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bots_are_told_apart_from_people():
    t = _traffic()
    assert t.is_bot("") and t.is_bot("Mozilla/5.0 (compatible; Googlebot/2.1)") and t.is_bot("python-requests/2.31")
    assert t.is_bot("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.1)")
    assert not t.is_bot("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36")


def test_day_windows_are_utc_days_newest_first_and_never_in_the_future():
    t = _traffic()
    now = 1_760_000_000.0           # 2025-10-09 08:53:20 UTC
    w = t.day_windows(3, now)
    assert [a[:10] for a, _ in w] == ["2025-10-09", "2025-10-08", "2025-10-07"]
    assert w[0][1] == "2025-10-09T08:53:20Z" and w[1][1] == "2025-10-09T00:00:00Z"


# --- where signups said they came from --------------------------------------------------------------

def test_source_counts_are_summed_per_answer_and_ignore_real_users():
    usage = [
        {"PartitionKey": "signup-source:reddit", "RowKey": "2026-10", "count": 3},
        {"PartitionKey": "signup-source:reddit", "RowKey": "2026-09", "count": 1},
        {"PartitionKey": "signup-source:claude_directory", "RowKey": "2026-10", "count": 5},
        {"PartitionKey": "signup-source:made_up", "RowKey": "2026-10", "count": "2"},
        {"PartitionKey": "u1", "RowKey": "2026-10", "count": 99},
    ]
    got = du.source_counts(usage, "2026-10")
    assert [r["source"] for r in got] == ["Claude's connector directory", "Reddit", "made_up"] or got[0]["month"] == 5
    by = {r["source"]: r for r in got}
    assert by["Reddit"] == {"source": "Reddit", "month": 3, "total": 4}
    assert by["Claude's connector directory"]["month"] == 5 and "u1" not in str(got)
    assert du.source_counts([], "2026-10") == []


def test_sources_appear_in_both_versions_of_the_email_and_are_escaped():
    r = rows()
    s = du.compare(r, {})
    sources = [{"source": "<b>x</b>", "month": 1, "total": 2}, {"source": "Reddit", "month": 3, "total": 4}]
    text, page = du.render_text(r, s, sources), du.render_html(r, s, sources)
    assert "Reddit: 3 this month, 4 in total" in text and "How people said they found us" in page
    assert "<b>x</b>" not in page and "&lt;b&gt;x&lt;/b&gt;" in page
    assert "found us" not in du.render_text(r, s) and "found us" not in du.render_html(r, s)



# --- the service line: free tier status and how many copies ran -----------------------------------

def test_service_line_reports_free_tier_and_peak_copies():
    assert du.service_line(None) == "" and du.service_line({"max_copies": 5}) == ""
    assert du.service_line({"free_open": True, "peak_copies": 2, "max_copies": 5}) == \
        "Service: free tier open, most copies running in the last 24 h: 2 of 5"
    assert "PAUSED" in du.service_line({"free_open": False}) and "Pro unaffected" in du.service_line({"free_open": False})
    r = rows()
    s = du.compare(r, {})
    svc = {"free_open": True, "peak_copies": 3, "max_copies": 5}
    assert "most copies running in the last 24 h: 3 of 5" in du.render_text(r, s, None, svc)
    assert "most copies running in the last 24 h: 3 of 5" in du.render_html(r, s, None, svc)
    assert "Service:" not in du.render_text(r, s)


def test_report_cap_matches_the_deploy_script():
    import re
    deploy = (Path(__file__).resolve().parents[1] / "ops" / "deploy.sh").read_text(encoding="utf-8")
    assert re.search(r'MAX_REPLICAS="\$\{DEPLOY_MAX_REPLICAS:-(\d+)\}"', deploy).group(1) == str(du.MAX_COPIES)
