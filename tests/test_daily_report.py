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
