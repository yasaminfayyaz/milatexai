"""Decision rules for the automatic repair. No network, no clock, no shell.

Everything that decides WHETHER an action may run lives here, so the tests can
pin it down. The AI only ever proposes (see ops/incident/triage_schema.json);
plan_action() below is the single gate between a proposal and a real change.

The principles, in order:
  1. Only actions on a short menu can run, each with its own preconditions.
  2. Hard limits stop a repair loop (actions per hour, one rollback a day, nothing
     while a deploy is running, never repeat a restart that did not help).
  3. The kill switch (AUTOFIX_ENABLED) turns every action into a dry run.
  4. Anything unclear, unusual or refused becomes "tell the owner", never a guess.
"""

from __future__ import annotations

import json
import re

ACTIONS = ("none", "restart", "rollback", "purge_cache", "disable_waf_rule", "escalate")
CATEGORIES = ("bad_deploy", "process_hang", "resource_exhaustion", "dependency_ours",
              "dependency_external", "edge_config", "traffic_attack", "unknown")
CONFIDENCE = ("low", "medium", "high")

MAX_ACTIONS_PER_HOUR = 3
MAX_RESTARTS_PER_HOUR = 2
MAX_PURGES_PER_HOUR = 2
MAX_ROLLBACKS_PER_DAY = 1
ROLLBACK_WINDOW_SECONDS = 6 * 3600     # only roll back a version that went live this recently
OPT_IN_PREFIX = "[auto-disableable]"   # a firewall rule must carry this in its description to ever be switched off
HOUR, DAY = 3600, 86400
DEPLOY_WINDOW_SECONDS = 20 * 60        # two live revisions this soon after the newest one started = a deploy in flight

# What the owner reads for each action.
PHRASES = {
    "restart": "restarted the app",
    "rollback": "rolled back to the previous version",
    "purge_cache": "cleared the Cloudflare cache",
    "disable_waf_rule": "switched off a firewall rule that was blocking real traffic",
}


def clip(value, limit: int) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text).strip()
    return text[:limit]


def parse_decision(raw) -> dict | None:
    """Turn the AI's output into a clean decision, or None if it is unusable.

    The input is untrusted (it was shaped by log text), so unknown keys are dropped,
    enums are enforced and every string is bounded."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    action = raw.get("action")
    if action not in ACTIONS:
        return None
    category = raw.get("category") if raw.get("category") in CATEGORIES else "unknown"
    confidence = raw.get("confidence") if raw.get("confidence") in CONFIDENCE else "low"
    return {
        "action": action,
        "category": category,
        "confidence": confidence,
        "diagnosis": clip(raw.get("diagnosis"), 600),
        "customer_impact": clip(raw.get("customer_impact"), 300),
        "waf_rule_id": clip(raw.get("waf_rule_id"), 64),
        "suggested_followup": clip(raw.get("suggested_followup"), 600),
    }


def count_recent(ledger: list[dict], now: float, window: int, action: str | None = None) -> int:
    return sum(1 for e in ledger if now - float(e.get("ts", 0)) < window and e.get("ok") is not False
               and (action is None or e.get("action") == action))


def parse_deploy_tags(tags: dict | None) -> dict:
    """ops/deploy.sh stamps the container app with the version it just made live
    (current_sha, deployed_at) and the one it replaced (previous_sha), but only after
    both smoke tests passed. That makes "previous_sha" a version that really served
    customers, which is what a rollback needs."""
    tags = tags or {}
    sha = lambda v: v if isinstance(v, str) and re.fullmatch(r"[0-9a-f]{40}", v) else ""
    try:
        deployed_at = float(tags.get("deployed_at")) if tags.get("deployed_at") else None
    except (TypeError, ValueError):
        deployed_at = None
    return {"current": sha(tags.get("current_sha")), "previous": sha(tags.get("previous_sha")), "deployed_at": deployed_at}


def deploy_in_flight(revisions: list[dict], now: float) -> bool:
    """ops/deploy.sh keeps exactly one revision active; two active at once means a
    deploy is mid-way (new one under test, old one still serving). If a stuck extra
    revision lingers longer than the window, it is leftover cost, not a deploy."""
    active = [r for r in revisions if r.get("active")]
    if len(active) < 2:
        return False
    return now - max(float(r.get("created", 0)) for r in active) < DEPLOY_WINDOW_SECONDS


def plan_action(decision: dict | None, facts: dict) -> dict:
    """Decide whether the proposed action may run.

    Returns {"do": <action or "none">, "mode": "run" | "dry_run" | "refused" | "hold" | "noop",
             "why": <plain sentence>, "target": <sha or rule id, when relevant>}.
    """
    def out(do, mode, why, target=""):
        return {"do": do, "mode": mode, "why": why, "target": target}

    if decision is None:
        return out("none", "refused", "The diagnosis was unusable, so nothing was changed.")
    action = decision["action"]
    if action in ("none", "escalate"):
        return out("none", "noop", "The diagnosis says this needs a person, not an automatic change.")

    kind = facts.get("kind", "ours")
    now = float(facts.get("now", 0))
    ledger = facts.get("ledger", [])
    drill = kind == "drill"
    ours = kind in ("ours", "drill")
    edge = kind in ("edge", "drill")

    if action in ("restart", "rollback") and not ours:
        return out("none", "refused", "The app answers fine directly, so restarting or rolling back would not help.")
    if action in ("purge_cache", "disable_waf_rule") and not edge:
        return out("none", "refused", "Customers can reach the site, so a Cloudflare change is not justified.")

    if facts.get("deploy_running") and action in ("restart", "rollback"):
        return out("none", "hold", "A deploy is running; it has its own automatic rollback, so no extra change was made.")

    if count_recent(ledger, now, HOUR) >= MAX_ACTIONS_PER_HOUR:
        return out("none", "refused", "Already made %d automatic changes in the last hour; stopping so repairs cannot loop."
                   % MAX_ACTIONS_PER_HOUR)

    target = ""
    if action == "restart":
        if facts.get("firstaid_restarted"):
            return out("none", "refused", "The app was already restarted for this incident and it did not help.")
        if count_recent(ledger, now, HOUR, "restart") >= MAX_RESTARTS_PER_HOUR:
            return out("none", "refused", "Restarted twice in the last hour already.")
    elif action == "purge_cache":
        if count_recent(ledger, now, HOUR, "purge_cache") >= MAX_PURGES_PER_HOUR:
            return out("none", "refused", "Cleared the cache twice in the last hour already.")
    elif action == "rollback":
        if count_recent(ledger, now, DAY, "rollback") >= MAX_ROLLBACKS_PER_DAY:
            return out("none", "refused", "Already rolled back once in the last 24 hours.")
        target = facts.get("rollback_target") or ""
        if not re.fullmatch(r"[0-9a-f]{40}", target):
            return out("none", "refused", "There is no earlier known-good version to go back to.")
        if not drill:
            deployed_at = facts.get("current_deployed_at")
            if deployed_at is None or now - float(deployed_at) > ROLLBACK_WINDOW_SECONDS:
                return out("none", "refused", "The running version has been live for hours, so a bad deploy is unlikely to be the cause.")
    elif action == "disable_waf_rule":
        rule_id = decision.get("waf_rule_id", "")
        rule = next((r for r in facts.get("waf_rules", []) if r.get("id") == rule_id), None)
        if rule is None:
            return out("none", "refused", "The firewall rule named in the diagnosis does not exist.")
        if not str(rule.get("description", "")).lower().startswith(OPT_IN_PREFIX):
            return out("none", "refused", "That firewall rule protects the site and is not marked as safe to switch off automatically.")
        if not rule.get("enabled", True):
            return out("none", "refused", "That firewall rule is already off.")
        target = rule_id

    if not facts.get("armed"):
        return out(action, "dry_run", "Automatic repair is switched off, so this was only recorded: %s." % PHRASES[action], target)
    return out(action, "run", "Allowed: %s." % PHRASES[action], target)


def compose_report(ctx: dict) -> dict:
    """The words that go to the owner. ctx keys: incident_id, kind, failing, healthy_now,
    armed, drill, firstaid {recovered, restarted, summary}, decision, plan, result {ran, ok, detail}."""
    fa = ctx.get("firstaid") or {}
    decision = ctx.get("decision")
    plan = ctx.get("plan") or {}
    result = ctx.get("result") or {}
    prefix = "[DRILL] " if ctx.get("drill") else ""
    # `details` goes in the private email. `public_details` is what a public GitHub issue may
    # carry: only fixed wording from our own scripts, never text the AI wrote from logs.
    ai_lines, plain_lines = [], []
    if decision and decision.get("diagnosis"):
        ai_lines.append("Diagnosis (%s confidence): %s" % (decision["confidence"], decision["diagnosis"]))
    if decision and decision.get("customer_impact"):
        ai_lines.append("Customer impact: " + decision["customer_impact"])
    if fa.get("summary"):
        plain_lines.append("First aid: " + fa["summary"])
    if plan.get("why"):
        plain_lines.append("Decision: " + plan["why"])
    if result.get("detail"):
        plain_lines.append("Result: " + result["detail"])
    if decision and decision.get("suggested_followup"):
        ai_lines.append("Suggested follow-up: " + decision["suggested_followup"])
    details = ai_lines[:2] + plain_lines + ai_lines[2:]
    public_details = list(plain_lines)

    healthy = bool(ctx.get("healthy_now"))
    done = None
    if fa.get("recovered") and fa.get("restarted"):
        done = PHRASES["restart"]
    elif result.get("ran") and result.get("ok"):
        done = PHRASES.get(plan.get("do"), "made a change")

    # "summary" and "public" are shown on the public status page, so they say only what
    # happened in plain words; the headline and details are for the owner.
    if healthy and done:
        return {"stage": "Fixed", "headline": prefix + "Fixed automatically: " + done, "details": details, "public_details": public_details,
                "resolved": True, "summary": prefix + done[0].upper() + done[1:], "public": ""}
    if healthy:
        return {"stage": "Recovered", "headline": prefix + "Recovered on its own, no repair was needed", "details": details,
                "public_details": public_details, "resolved": True, "summary": prefix + "Recovered on its own", "public": ""}
    if not ctx.get("armed") and not ctx.get("drill"):
        head = "Needs you: automatic repair is switched off"
    elif plan.get("mode") in ("refused", "hold"):
        head = "Needs you: " + plan.get("why", "the automatic repair stopped")
    elif result.get("ran") and not result.get("ok"):
        head = "Needs you: the automatic repair did not work"
    elif result.get("ran"):
        head = "Needs you: the automatic repair did not fix it"
    else:
        head = "Needs you: this is not something that can be fixed automatically"
    return {"stage": "Needs you", "headline": prefix + head, "details": details, "public_details": public_details, "resolved": False,
            "summary": "", "public": prefix + "We are looking into a problem and working on a fix"}


REDACTIONS = (
    (re.compile(r"\?[^\s\"']*"), "?[query removed]"),                       # query strings can carry codes and tokens
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "x.x.x.x"),               # visitor addresses
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [redacted]"),
    (re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"), "[redacted]"),               # anything that looks like a key or token
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
)


def redact(text: str, limit: int = 400) -> str:
    out = str(text)
    for pattern, repl in REDACTIONS:
        out = pattern.sub(repl, out)
    return clip(out, limit)
