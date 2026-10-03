"""Decision logic of the Cloudflare watchdog (ops/watchdog/worker.js).

The Worker's pure functions are executed here with QuickJS. This is the part
that decides when to alert, when to start a repair, and when to say
"recovered", so it is tested for the classic monitoring failures: crying wolf
on one bad minute, spamming during a long outage, never closing an incident,
and letting text that originated in logs inject HTML into the public page.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

quickjs = pytest.importorskip("quickjs")

SRC = Path(__file__).resolve().parents[1] / "ops" / "watchdog" / "worker.js"
T0 = 1_760_000_000_000
MIN = 60_000


@pytest.fixture(scope="module")
def js():
    code = SRC.read_text(encoding="utf-8")
    assert "export default {" in code and "export const __test =" in code
    code = code.replace("export default {", "var __default = {", 1).replace("export const __test =", "var __test =", 1)
    ctx = quickjs.Context()
    ctx.eval(code)

    def call(expr: str):
        return json.loads(ctx.eval(f"JSON.stringify({expr})"))

    return call


def jsv(v) -> str:
    return json.dumps(v)


def classify(js, **r):
    base = {"site": {"ok": True}, "connector": {"ok": True}, "signin": {"ok": True}, "origin": {"ok": True},
            "app": {"reachable": True, "ok": True, "failingOurs": [], "failingExt": [], "degraded": False}}
    base.update(r)
    return js(f"__test.classify({jsv(base)})")


def step(js, state, kind, minute, failing=None):
    cls = {"kind": kind, "failing": failing if failing is not None else ([] if kind == "none" else ["origin"]), "version": "abc"}
    out = js(f"__test.step({jsv(state)}, {jsv(cls)}, {T0 + minute * MIN})")
    return out["state"], out["actions"]


def kinds(actions):
    return [(a["type"], a.get("template")) for a in actions]


# --- classify ------------------------------------------------------------------

def test_healthy(js):
    assert classify(js)["kind"] == "none"


def test_app_server_down_is_ours(js):
    c = classify(js, origin={"ok": False}, site={"ok": False}, connector={"ok": False})
    assert c["kind"] == "ours" and c["failing"] == ["origin"]


def test_broken_database_is_ours_and_named(js):
    c = classify(js, app={"reachable": True, "ok": False, "failingOurs": ["storage"], "failingExt": []})
    assert c["kind"] == "ours" and c["failing"] == ["app:storage"]


def test_customers_cannot_reach_but_app_is_fine_is_edge(js):
    assert classify(js, site={"ok": False})["kind"] == "edge"
    assert classify(js, connector={"ok": False})["kind"] == "edge"
    c = classify(js, app={"reachable": False, "ok": False, "failingOurs": [], "failingExt": []})
    assert c["kind"] == "edge" and "app:unreachable" in c["failing"]


def test_third_party_trouble_is_external_not_ours(js):
    assert classify(js, signin={"ok": False})["kind"] == "external"
    c = classify(js, app={"reachable": True, "ok": True, "degraded": True, "failingOurs": [], "failingExt": ["overleaf_git"]})
    assert c["kind"] == "external" and c["failing"] == ["ext:overleaf_git"]


def test_ours_outranks_external(js):
    c = classify(js, origin={"ok": False}, signin={"ok": False})
    assert c["kind"] == "ours"


def test_parse_deep(js):
    assert js("__test.parseDeep(404, '')")["ok"] is True  # older image without the endpoint
    bad = js("__test.parseDeep(200, 'not json')")
    assert bad["ok"] is False and bad["reachable"] is True and bad["failingOurs"] == ["response"]
    body = json.dumps({"ok": False, "version": "v", "checks": {"storage": {"ok": False, "scope": "ours"},
                                                               "payments": {"ok": False, "scope": "external"}}})
    p = js(f"__test.parseDeep(503, {jsv(body)})")
    assert p["ok"] is False and p["failingOurs"] == ["storage"] and p["failingExt"] == ["payments"] and p["degraded"] is True


# --- the alert / repair state machine ----------------------------------------------

def test_one_bad_minute_does_not_cry_wolf(js):
    s, a = step(js, None, "ours", 0)
    assert a == [] and s["incident"] is None
    s, a = step(js, s, "none", 1)
    s, a = step(js, s, "ours", 2)  # flapping resets the counter
    assert a == [] and s["incident"] is None


def test_two_bad_minutes_open_exactly_one_incident(js):
    s, _ = step(js, None, "ours", 0)
    s, a = step(js, s, "ours", 1)
    assert kinds(a) == [("email", "opened"), ("dispatch", None)] and s["incident"]["dispatches"] == 1
    s, a = step(js, s, "ours", 2)
    s, a2 = step(js, s, "ours", 3)
    assert a == [] and a2 == []  # no spam while it is ongoing


def test_reminders_and_limited_retries_during_a_long_outage(js):
    s, _ = step(js, None, "ours", 0)
    s, _ = step(js, s, "ours", 1)
    emails, dispatches = [], 1
    for m in range(2, 95):
        s, a = step(js, s, "ours", m)
        emails += [x for x in a if x["type"] == "email"]
        dispatches += sum(1 for x in a if x["type"] == "dispatch")
    assert dispatches == 3                      # opened + 2 retries, then it stops asking
    assert 4 <= len(emails) <= 8                # a reminder about every 15 minutes, not every minute
    assert all(e["template"] == "still_down" for e in emails)


def test_recovery_needs_three_good_minutes_and_says_so(js):
    s, _ = step(js, None, "ours", 0)
    s, _ = step(js, s, "ours", 1)
    s, a = step(js, s, "none", 2)
    s, a2 = step(js, s, "none", 3)
    assert a == [] and a2 == [] and s["incident"] is not None
    s, _ = step(js, s, "ours", 4)               # relapse resets the good-minute count
    assert s["oks"] == 0
    for m in (5, 6):
        s, a = step(js, s, "none", m)
        assert a == []
    s, a = step(js, s, "none", 7)
    assert kinds(a) == [("email", "recovered")] and s["incident"] is None
    assert len(s["history"]) == 1 and s["history"][0]["kind"] == "ours"


def test_edge_problems_also_trigger_repair(js):
    s, _ = step(js, None, "edge", 0, ["site"])
    s, a = step(js, s, "edge", 1, ["site"])
    assert ("dispatch", None) in kinds(a) and s["incident"]["kind"] == "edge"


def test_third_party_outage_emails_once_and_never_starts_a_repair(js):
    s = None
    all_actions = []
    for m in range(0, 30):
        s, a = step(js, s, "external", m, ["ext:overleaf_git"])
        all_actions += a
    assert kinds(all_actions) == [("email", "external")]
    assert s["incident"] is None and s["fails"] == 0
    s, a = step(js, s, "none", 30)
    assert kinds(a) == [("email", "external_recovered")] and s["ext"] is None


def test_short_third_party_blip_stays_silent(js):
    s = None
    acts = []
    for m in range(0, 4):
        s, a = step(js, s, "external", m, ["ext:payments"])
        acts += a
    s, a = step(js, s, "none", 4)
    assert acts == [] and a == []


def test_failing_set_updates_without_extra_alerts(js):
    s, _ = step(js, None, "ours", 0)
    s, _ = step(js, s, "ours", 1, ["origin"])
    s, a = step(js, s, "ours", 2, ["origin", "app:latex"])
    assert a == [] and s["incident"]["failing"] == ["origin", "app:latex"]


# --- bookkeeping ---------------------------------------------------------------------

def test_bar_marks_skipped_minutes_and_is_capped(js):
    assert js(f"__test.updateBar('', 0, {T0}, 'g')") == "g"
    assert js(f"__test.updateBar('g', {T0}, {T0 + 3 * MIN}, 'r')") == "g..r"
    long = "g" * 1440
    out = js(f"__test.updateBar({jsv(long)}, {T0}, {T0 + MIN}, 'r')")
    assert len(out) == 1440 and out.endswith("r")


def test_uptime_percentage(js):
    days = js(f"(function(){{ let d = {{}}; for (let i = 0; i < 100; i++) d = __test.updateDays(d, {T0} + i * 60000, i < 1); return d; }})()")
    pct = js(f"__test.uptimePercent({jsv(days)}, {T0 + 99 * MIN}, 30)")
    assert pct == 99.0
    assert js("__test.uptimePercent({}, 0, 30)") is None


def test_duration_formatting(js):
    assert js("__test.fmtDuration(30000)") == "1 minute"
    assert js(f"__test.fmtDuration({125 * MIN})") == "2 hours 5 min"


# --- public status page --------------------------------------------------------------

def healthy_core():
    return {"fails": 0, "oks": 5, "incident": None, "ext": None, "history": [], "lastRun": T0, "days": {},
            "bar": "g" * 60, "latest": {"ts": T0, "kind": "none", "version": "abcdef1234", "results": {
                "site": {"ok": True}, "connector": {"ok": True}, "signin": {"ok": True}, "origin": {"ok": True},
                "app": {"ok": True, "reachable": True, "checks": {
                    "storage": {"ok": True, "scope": "ours"}, "latex": {"ok": True, "scope": "ours"},
                    "signin_keys": {"ok": True, "scope": "external"}, "payments": {"ok": True, "skipped": True, "scope": "external"},
                    "overleaf_git": {"ok": True, "scope": "external"}}}}}}


def test_status_page_healthy(js):
    html = js(f"__test.renderStatus({jsv(healthy_core())}, {T0}, {{}})")
    assert html.startswith("<!doctype html>") and "All systems operational" in html
    for name in ("Website", "Connector (MCP)", "Database", "LaTeX compiling"):
        assert name in html
    assert "abcdef1" in html and "abcdef1234" not in html


def test_status_page_shows_an_active_incident(js):
    core = healthy_core()
    core["incident"] = {"id": "inc-1", "openedAt": T0, "kind": "ours", "failing": ["origin"], "headline": "Restarting the app"}
    html = js(f"__test.renderStatus({jsv(core)}, {T0}, {{}})")
    assert "We are fixing a problem" in html and "Restarting the app" in html


def test_status_page_cannot_be_used_for_html_injection(js):
    core = healthy_core()
    evil = '<script>alert(1)</script><img src=x onerror=alert(2)>'
    core["incident"] = {"id": "inc-1", "openedAt": T0, "kind": "ours", "failing": [], "headline": evil}
    core["history"] = [{"id": "inc-0", "openedAt": T0 - 3600000, "closedAt": T0 - 3000000, "kind": "ours", "failing": [], "summary": evil}]
    html = js(f"__test.renderStatus({jsv(core)}, {T0}, {{}})")
    assert "<script>" not in html and "<img src=x" not in html
    assert "&lt;script&gt;" in html


# --- emails --------------------------------------------------------------------------

def test_every_email_template_is_clean_and_links_to_status(js):
    inc = {"id": "inc-1", "openedAt": T0, "kind": "ours", "failing": ["origin", "app:storage"], "dispatches": 2}
    ctxs = {
        "opened": {"incident": inc}, "still_down": {"incident": inc, "now": T0 + 40 * MIN},
        "recovered": {"incident": inc, "closedAt": T0 + 12 * MIN},
        "external": {"ext": {"failing": ["ext:overleaf_git"]}}, "external_recovered": {"ext": {"failing": ["ext:overleaf_git"]}},
        "report": {"stage": "Fixed", "headline": "Restarted the app", "details": ["a", "b"], "links": ["https://x.example/y"]},
    }
    for template, ctx in ctxs.items():
        m = js(f"__test.composeEmail({jsv(template)}, {jsv(ctx)}, {{}})")
        assert m["subject"].startswith("[MiLatexAI]") and "https://status.milatexai.com" in m["text"], template
        assert "—" not in m["subject"] + m["text"], template
    opened = js(f"__test.composeEmail('opened', {jsv(ctxs['opened'])}, {{}})")
    assert "the app server" in opened["subject"] and "the database" in opened["subject"]
    assert "needs you" in js(f"__test.composeEmail('still_down', {jsv(ctxs['still_down'])}, {{}})")["text"]


def test_report_payloads_are_bounded_and_cleaned(js):
    body = {"incident_id": "x" * 500, "headline": "h\u0000\u0007" + "y" * 999, "details": ["d" * 999] * 100,
            "links": ["https://ok.example/a", "javascript:alert(1)", "http://plain.example"], "resolved": True}
    r = js(f"__test.cleanReport({jsv(body)})")
    assert len(r["incident_id"]) == 60 and len(r["headline"]) == 200 and "\u0000" not in r["headline"]
    assert len(r["details"]) == 40 and all(len(d) == 300 for d in r["details"])
    assert r["links"] == ["https://ok.example/a"] and r["resolved"] is True


def test_constant_time_equal(js):
    assert js("__test.constantTimeEqual('abc', 'abc')") is True
    assert js("__test.constantTimeEqual('abc', 'abd')") is False
    assert js("__test.constantTimeEqual('abc', 'ab')") is False
    assert js("__test.constantTimeEqual('', undefined)") is False
