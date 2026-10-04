"""The gate between "the AI proposes" and "a real change happens" (ops/incident/plan.py).

These tests are the safety case for letting the repair run unattended: the AI can
only pick from a short menu, every pick has preconditions, hard limits stop a loop,
and the kill switch turns every action into a dry run.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "incident_plan", Path(__file__).resolve().parents[1] / "ops" / "incident" / "plan.py")
plan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plan)

NOW = 1_760_000_000.0
GOOD = "a" * 40
CUR = "b" * 40


def decision(action="restart", **kw):
    base = {"action": action, "category": "process_hang", "confidence": "high", "diagnosis": "d", "waf_rule_id": ""}
    base.update(kw)
    return plan.parse_decision(base)


def facts(**kw):
    base = {"armed": True, "kind": "ours", "now": NOW, "ledger": [], "deploy_running": False, "firstaid_restarted": False,
            "rollback_target": GOOD, "current_deployed_at": NOW - 600, "waf_rules": []}
    base.update(kw)
    return base


def entry(action, ago=60, ok=True):
    return {"ts": NOW - ago, "action": action, "ok": ok}


# --- parsing the AI's output ---------------------------------------------------------

def test_unusable_output_is_rejected():
    for raw in (None, "", "not json", "[]", 5, {}, {"action": "format_disk"}, {"action": "RESTART"}, '{"action": "rm -rf"}'):
        assert plan.parse_decision(raw) is None


def test_output_is_cleaned_and_bounded():
    d = plan.parse_decision({"action": "restart", "category": "made up", "confidence": "certain", "diagnosis": "x" * 5000,
                             "customer_impact": "y" * 5000, "waf_rule_id": "z" * 500, "extra": "ignored", "command": "curl evil"})
    assert d["category"] == "unknown" and d["confidence"] == "low"
    assert len(d["diagnosis"]) == 600 and len(d["customer_impact"]) == 300 and len(d["waf_rule_id"]) == 64
    assert "extra" not in d and "command" not in d
    assert plan.parse_decision('{"action": "none"}')["action"] == "none"


# --- the menu and its preconditions ---------------------------------------------------

def test_no_decision_means_no_change():
    p = plan.plan_action(None, facts())
    assert p["do"] == "none" and p["mode"] == "refused"


@pytest.mark.parametrize("action", ["none", "escalate"])
def test_asking_for_a_human_changes_nothing(action):
    assert plan.plan_action(decision(action), facts())["mode"] == "noop"


def test_restart_runs_for_a_problem_of_ours():
    p = plan.plan_action(decision("restart"), facts())
    assert (p["do"], p["mode"]) == ("restart", "run")


def test_restart_and_rollback_refused_when_the_app_is_fine_and_the_edge_is_broken():
    for action in ("restart", "rollback"):
        p = plan.plan_action(decision(action), facts(kind="edge"))
        assert p["mode"] == "refused", action


def test_cloudflare_changes_refused_when_customers_can_reach_the_site():
    for action in ("purge_cache", "disable_waf_rule"):
        assert plan.plan_action(decision(action), facts(kind="ours"))["mode"] == "refused", action
    assert plan.plan_action(decision("purge_cache"), facts(kind="edge"))["mode"] == "run"


def test_nothing_changes_while_a_deploy_is_running():
    p = plan.plan_action(decision("restart"), facts(deploy_running=True))
    assert p["mode"] == "hold"
    assert plan.plan_action(decision("rollback"), facts(deploy_running=True))["mode"] == "hold"


# --- the kill switch --------------------------------------------------------------------

def test_kill_switch_turns_every_action_into_a_dry_run():
    rules = [{"id": "r1", "description": plan.OPT_IN_PREFIX + " test", "enabled": True}]
    cases = [("restart", "ours"), ("rollback", "ours"), ("purge_cache", "edge"), ("disable_waf_rule", "edge")]
    for action, kind in cases:
        p = plan.plan_action(decision(action, waf_rule_id="r1"), facts(armed=False, kind=kind, waf_rules=rules))
        assert (p["do"], p["mode"]) == (action, "dry_run"), action


# --- hard limits -------------------------------------------------------------------------

def test_three_actions_in_an_hour_stops_the_loop():
    # three different actions, so no per-action limit can be what stops the fourth
    ledger = [entry("restart", 100), entry("purge_cache", 900), entry("disable_waf_rule", 3000)]
    p = plan.plan_action(decision("purge_cache"), facts(kind="edge", ledger=ledger))
    assert p["mode"] == "refused" and "3 automatic changes" in p["why"]
    assert plan.plan_action(decision("purge_cache"), facts(kind="edge", ledger=ledger[:2]))["mode"] == "run"
    old = [entry("restart", 4000), entry("restart", 5000), entry("restart", 6000)]
    assert plan.plan_action(decision("restart"), facts(ledger=old))["mode"] == "run"  # older than an hour does not count


def test_failed_attempts_do_not_burn_the_budget():
    ledger = [entry("restart", 60, ok=False)] * 5
    assert plan.plan_action(decision("restart"), facts(ledger=ledger))["mode"] == "run"


def test_a_restart_that_already_failed_is_not_repeated():
    p = plan.plan_action(decision("restart"), facts(firstaid_restarted=True))
    assert p["mode"] == "refused" and "did not help" in p["why"]


def test_at_most_two_restarts_an_hour():
    ledger = [entry("restart", 300), entry("restart", 1500)]
    assert plan.plan_action(decision("restart"), facts(ledger=ledger))["mode"] == "refused"


def test_rollback_needs_a_known_good_version_and_a_recent_deploy():
    assert plan.plan_action(decision("rollback"), facts())["mode"] == "run"
    assert plan.plan_action(decision("rollback"), facts(rollback_target=""))["mode"] == "refused"
    assert plan.plan_action(decision("rollback"), facts(rollback_target="not-a-sha"))["mode"] == "refused"
    old = facts(current_deployed_at=NOW - 7 * 3600)
    assert "hours" in plan.plan_action(decision("rollback"), old)["why"]
    assert plan.plan_action(decision("rollback"), facts(current_deployed_at=None))["mode"] == "refused"
    assert plan.plan_action(decision("rollback"), facts(kind="drill", current_deployed_at=None))["mode"] == "run"


def test_rollback_target_comes_from_the_facts_not_from_the_ai():
    d = decision("rollback")
    d["target"] = "c" * 40   # a field the AI is not allowed to supply
    assert plan.plan_action(d, facts())["target"] == GOOD


def test_one_rollback_a_day():
    ledger = [entry("rollback", 3 * 3600)]
    assert plan.plan_action(decision("rollback"), facts(ledger=ledger))["mode"] == "refused"
    ledger = [entry("rollback", 25 * 3600)]
    assert plan.plan_action(decision("rollback"), facts(ledger=ledger))["mode"] == "run"


# --- firewall rules ------------------------------------------------------------------------

def test_only_rules_marked_safe_can_be_switched_off():
    rules = [
        {"id": "protect", "description": "Block scanners", "enabled": True},
        {"id": "geo", "description": "Geo Block", "enabled": True},
        {"id": "ok", "description": plan.OPT_IN_PREFIX.upper() + " temporary rule", "enabled": True},
        {"id": "off", "description": plan.OPT_IN_PREFIX + " already off", "enabled": False},
    ]
    f = facts(kind="edge", waf_rules=rules)
    assert plan.plan_action(decision("disable_waf_rule", waf_rule_id="protect"), f)["mode"] == "refused"
    assert plan.plan_action(decision("disable_waf_rule", waf_rule_id="geo"), f)["mode"] == "refused"
    assert plan.plan_action(decision("disable_waf_rule", waf_rule_id="missing"), f)["mode"] == "refused"
    assert plan.plan_action(decision("disable_waf_rule", waf_rule_id="off"), f)["mode"] == "refused"
    assert plan.plan_action(decision("disable_waf_rule", waf_rule_id=""), f)["mode"] == "refused"
    p = plan.plan_action(decision("disable_waf_rule", waf_rule_id="ok"), f)
    assert (p["do"], p["mode"], p["target"]) == ("disable_waf_rule", "run", "ok")


# --- deploy tags -----------------------------------------------------------------------------

def test_deploy_tags_are_validated():
    t = plan.parse_deploy_tags({"current_sha": CUR, "previous_sha": GOOD, "deployed_at": "1760000000"})
    assert t == {"current": CUR, "previous": GOOD, "deployed_at": 1760000000.0}
    bad = plan.parse_deploy_tags({"current_sha": "evil; rm -rf /", "previous_sha": "xyz", "deployed_at": "soon"})
    assert bad == {"current": "", "previous": "", "deployed_at": None}
    assert plan.parse_deploy_tags(None)["previous"] == ""


# --- what the owner reads --------------------------------------------------------------------

def report(**kw):
    base = {"healthy_now": False, "armed": True, "drill": False, "firstaid": {}, "decision": decision("restart"),
            "plan": {"do": "restart", "mode": "run", "why": "Allowed: restarted the app."}, "result": {}}
    base.update(kw)
    return plan.compose_report(base)


def test_report_fixed_by_first_aid():
    r = report(healthy_now=True, firstaid={"recovered": True, "restarted": True, "summary": "Restarted; healthy after 40s."})
    assert r["resolved"] is True and r["stage"] == "Fixed" and "restarted the app" in r["headline"]
    assert r["summary"] == "Restarted the app"


def test_report_fixed_by_a_decided_action():
    r = report(healthy_now=True, plan={"do": "rollback", "mode": "run", "why": "Allowed."}, result={"ran": True, "ok": True, "detail": "ok"})
    assert r["resolved"] is True and "rolled back" in r["headline"]


def test_report_recovered_on_its_own():
    r = report(healthy_now=True, plan={}, decision=None)
    assert r["resolved"] is True and "on its own" in r["headline"]


def test_report_not_fixed_asks_for_the_owner():
    for kw, text in (
        ({"armed": False}, "switched off"),
        ({"plan": {"do": "none", "mode": "refused", "why": "Already rolled back once."}}, "Already rolled back once."),
        ({"result": {"ran": True, "ok": False, "detail": "x"}}, "did not work"),
        ({"result": {"ran": True, "ok": True, "detail": "x"}}, "did not fix it"),
        ({"plan": {"do": "none", "mode": "noop", "why": "n"}}, "not something that can be fixed automatically"),
    ):
        r = report(**kw)
        assert r["resolved"] is False and r["stage"] == "Needs you" and text in r["headline"], text
        # the public status page must never carry the owner-facing wording
        assert "Needs you" not in r["public"] and r["summary"] == "" and "switched off" not in r["public"]


def test_report_marks_drills_and_never_contains_an_em_dash():
    r = report(healthy_now=True, drill=True, firstaid={"recovered": True, "restarted": True})
    assert r["headline"].startswith("[DRILL]")
    text = " ".join([r["headline"], *r["details"]]) + " ".join(plan.PHRASES.values())
    assert chr(0x2014) not in text


# --- redaction ---------------------------------------------------------------------------------

def test_redaction_removes_secrets_and_personal_data():
    fake_key = "sk" + "_live_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"   # built in pieces so secret scanners ignore this test
    line = ('INFO 203.0.113.9:5555 - "GET /oauth/cb?code=abc123&state=zzz HTTP/1.1" 200 '
            'Authorization: Bearer abcdefghijklmnop1234 key=' + fake_key + ' mail=someone@example.com')
    out = plan.redact(line)
    for leaked in ("203.0.113.9", "abc123", "abcdefghijklmnop1234", fake_key, "someone@example.com"):
        assert leaked not in out
    assert "GET /oauth/cb" in out


def test_redaction_is_bounded():
    assert len(plan.redact("a b " * 500, 100)) == 100


# --- deploy detection ---------------------------------------------------------------------------

def test_a_deploy_in_flight_is_two_active_revisions_started_recently():
    one = [{"active": True, "created": NOW - 100}, {"active": False, "created": NOW - 900}]
    two = [{"active": True, "created": NOW - 100}, {"active": True, "created": NOW - 4000}]
    stuck = [{"active": True, "created": NOW - 3 * 3600}, {"active": True, "created": NOW - 5 * 3600}]
    assert plan.deploy_in_flight(one, NOW) is False
    assert plan.deploy_in_flight(two, NOW) is True
    assert plan.deploy_in_flight(stuck, NOW) is False
    assert plan.deploy_in_flight([], NOW) is False


# --- keeping model-written text off a public page ------------------------------------------------

def test_public_details_never_contain_text_the_model_wrote():
    d = decision("escalate", diagnosis="Stripe key sk_test rejected for customer jane", suggested_followup="rotate the key")
    d["customer_impact"] = "Jane cannot pay"
    r = plan.compose_report({"healthy_now": False, "armed": True, "drill": False, "firstaid": {"summary": "Restarted; still failing."},
                             "decision": d, "plan": {"do": "none", "mode": "noop", "why": "needs a person"}, "result": {}})
    private, public = " ".join(r["details"]), " ".join(r["public_details"])
    assert "jane" in private.lower() and "rotate the key" in private
    assert "jane" not in public.lower() and "Stripe" not in public and "rotate" not in public
    assert "Restarted; still failing." in public and "needs a person" in public


# --- reading the model's answer from the action's output file ---------------------------------------

def test_decision_is_extracted_from_the_execution_file(tmp_path):
    import json as _json
    spec = importlib.util.spec_from_file_location("save_decision", Path(__file__).resolve().parents[1] / "ops" / "incident" / "save_decision.py")
    sd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sd)
    answer = {"action": "restart", "category": "process_hang", "confidence": "high", "diagnosis": "d", "customer_impact": "c"}
    assert sd.find_structured([{"type": "system"}, {"type": "result", "structured_output": answer}]) == answer
    assert sd.find_structured({"type": "result", "structured_output": _json.dumps(answer)}) == answer
    assert sd.find_structured([{"type": "result", "result": "text only"}]) is None
    assert sd.find_structured([{"structured_output": "not json"}]) is None
    assert sd.find_structured("garbage") is None


# --- the check on the checker ------------------------------------------------------------------------

def _backstop():
    spec = importlib.util.spec_from_file_location("backstop", Path(__file__).resolve().parents[1] / "ops" / "incident" / "backstop.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backstop_only_raises_the_alarm_when_the_watchdog_is_really_silent():
    judge = _backstop().judge
    assert judge(200, '{"lastRun": 1, "ageSeconds": 75}', 0)[0] == "ok"
    assert judge(200, '{"ageSeconds": 1200}', 0)[0] == "stale"
    assert judge(200, '{"ageSeconds": null}', 0)[0] == "stale"
    assert judge(200, "not json at all {", 0)[0] == "unknown"      # a challenge page, not an answer
    assert judge(503, "", 0)[0] == "stale" and judge(0, "", 0)[0] == "stale"
    for blocked in (401, 403, 429):
        assert judge(blocked, "", 0)[0] == "unknown"               # Cloudflare turned the runner away: never alarm


# --- a "drill" label can never relax the rules on production ---------------------------------------------

def test_kill_switch_and_drill_labels(monkeypatch):
    spec = importlib.util.spec_from_file_location("incident_common", Path(__file__).resolve().parents[1] / "ops" / "incident" / "common.py")
    common = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(common)

    for key in ("DRILL_APP", "INCIDENT_KIND", "AUTOFIX_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    # production, kill switch off by default
    assert common.target_app() == "milatexai-app" and not common.is_drill() and not common.armed()
    monkeypatch.setenv("AUTOFIX_ENABLED", "TRUE")
    assert common.armed()
    monkeypatch.setenv("AUTOFIX_ENABLED", "yes")
    assert not common.armed()                                   # only the word "true" arms it
    # a request that says "drill" but names no drill app is handled as a real problem
    monkeypatch.setenv("INCIDENT_KIND", "drill")
    assert common.effective_kind() == "ours"
    monkeypatch.setenv("INCIDENT_KIND", "anything else")
    assert common.effective_kind() == "ours"
    monkeypatch.setenv("INCIDENT_KIND", "edge")
    assert common.effective_kind() == "edge"
    # a real rehearsal is armed without the switch and acts on the copy
    monkeypatch.setenv("DRILL_APP", "milatexai-drill")
    monkeypatch.setenv("AUTOFIX_ENABLED", "false")
    assert common.is_drill() and common.armed() and common.effective_kind() == "drill"


def test_only_the_drill_copy_can_be_named_as_a_drill_target(monkeypatch):
    spec = importlib.util.spec_from_file_location("incident_common2", Path(__file__).resolve().parents[1] / "ops" / "incident" / "common.py")
    common = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(common)
    monkeypatch.setenv("DRILL_APP", "some-other-app")
    with pytest.raises(SystemExit):
        common.target_app()
