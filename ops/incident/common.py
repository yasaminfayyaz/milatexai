"""Shared plumbing for the incident scripts: shell, HTTP, Azure, GitHub, Cloudflare.

Standard library only (the workflow does not install anything). Nothing here makes
decisions; the rules live in plan.py.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

RG = "milatexai-rg"
PROD_APP = "milatexai-app"
SITE = "https://milatexai.com"
STATUS = "https://status.milatexai.com"
REPO = os.environ.get("GITHUB_REPOSITORY", "yasaminfayyaz/milatexai")
CF_ACCOUNT = "ee666a4f2c1b7d4a6c75dbbd54ef9e5a"
CF_ZONE = "1378686c9478bd4880bca1288ead3079"
UA = "milatexai-incident/1.0"


def target_app() -> str:
    """The app every action applies to. A drill points this at a throwaway copy."""
    return os.environ.get("DRILL_APP", "").strip() or PROD_APP


def is_drill() -> bool:
    return target_app() != PROD_APP


def armed() -> bool:
    """The kill switch. In a drill the copy is disposable, so real actions are allowed;
    production changes need the repository variable AUTOFIX_ENABLED=true."""
    return is_drill() or os.environ.get("AUTOFIX_ENABLED", "").strip().lower() == "true"


def log(msg: str) -> None:
    print(msg, flush=True)


def set_output(name: str, value) -> None:
    text = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}<<__EOF__\n{text}\n__EOF__\n")
    log(f"output {name} = {text[:300]}")


def sh(cmd: list[str], timeout: int = 120, check: bool = True) -> str:
    cmd = [shutil.which(cmd[0]) or cmd[0], *cmd[1:]]   # "az" is az.cmd on Windows
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    if check and res.returncode != 0:
        raise RuntimeError(f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''} failed: {res.stderr.strip()[:300]}")
    return res.stdout


def az(*args: str, timeout: int = 180, check: bool = True):
    out = sh(["az", *args, "--only-show-errors", "-o", "json"], timeout=timeout, check=check)
    return json.loads(out) if out.strip() else None


def http(url: str, method: str = "GET", headers: dict | None = None, body: bytes | None = None,
         timeout: int = 20) -> tuple[int, dict, str]:
    req = urllib.request.Request(url, method=method, data=body, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001  (timeout, DNS, TLS: all mean "not reachable")
        return 0, {}, ""


# --- probing ---------------------------------------------------------------------------

def app_base(app: str | None = None) -> str:
    fqdn = az("containerapp", "show", "-n", app or target_app(), "-g", RG,
              "--query", "properties.configuration.ingress.fqdn")
    return f"https://{fqdn}"


def probe_origin(base: str, patience: int = 0) -> dict:
    """Is the app itself healthy, asked directly (not through Cloudflare)?

    `patience` seconds of retrying lets a scaled-to-zero or just-restarted copy boot."""
    deadline = time.time() + patience
    while True:
        res = _probe_once(base)
        if res["healthy"] or time.time() >= deadline:
            return res
        time.sleep(10)


def _probe_once(base: str) -> dict:
    res = {"healthy": False, "deep_status": 0, "failing": [], "version": ""}
    status, _, body = http(base + "/health/deep", timeout=45)
    res["deep_status"] = status
    if status == 404:                      # an older image without the endpoint
        status2, _, _ = http(base + "/health/capacity", timeout=45)
        res["healthy"] = status2 == 200
        res["failing"] = [] if res["healthy"] else ["origin"]
        return res
    try:
        deep = json.loads(body)
    except ValueError:
        res["failing"] = ["origin"] if status == 0 else ["app:response"]
        return res
    res["version"] = str(deep.get("version", ""))[:40]
    res["failing"] = [f"app:{k}" for k, c in (deep.get("checks") or {}).items()
                      if c.get("scope") == "ours" and not c.get("ok")]
    res["healthy"] = status == 200 and deep.get("ok") is True
    res["checks"] = {k: {"ok": c.get("ok"), "scope": c.get("scope"), "error": c.get("error")}
                     for k, c in (deep.get("checks") or {}).items()}
    return res


def site_view() -> dict:
    """What the watchdog (running inside Cloudflare) currently sees, via its public JSON."""
    status, _, body = http(STATUS + "/api/status.json", timeout=15)
    try:
        data = json.loads(body)
    except ValueError:
        return {"known": False}
    updated = data.get("updated") or 0
    return {"known": True, "level": (data.get("status") or {}).get("level"),
            "age_seconds": int(time.time() - updated / 1000) if updated else None}


# --- GitHub (through the gh CLI, authenticated by GH_TOKEN) -------------------------------

def gh_api(method: str, path: str, fields: dict | None = None) -> object:
    cmd = ["gh", "api", "-X", method, path]
    for key, value in (fields or {}).items():
        cmd += ["-f" if isinstance(value, str) else "-F", f"{key}={value}"]
    out = sh(cmd)
    return json.loads(out) if out.strip() else None


def ledger_issue() -> int:
    """The one issue that records every automatic change, so limits survive between runs."""
    found = gh_api("GET", f"repos/{REPO}/issues?labels=incident-ledger&state=all&per_page=1")
    if found:
        return found[0]["number"]
    try:
        gh_api("POST", f"repos/{REPO}/labels", {"name": "incident-ledger", "color": "6e7781",
                                                "description": "Record of automatic repairs"})
    except RuntimeError:
        pass  # label already exists
    made = gh_api("POST", f"repos/{REPO}/issues", {
        "title": "Automatic repair ledger",
        "body": "Machine-written record of every automatic change, used to enforce rate limits. Safe to ignore.",
        "labels[]": "incident-ledger"})
    return made["number"]


LEDGER_LINE = re.compile(r"^LEDGER (\{.*\})$", re.M)


def ledger_read(app: str, window: int = 86400) -> list[dict]:
    try:
        number = ledger_issue()
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - window))
        comments = gh_api("GET", f"repos/{REPO}/issues/{number}/comments?per_page=100&since={since}") or []
    except RuntimeError as exc:
        log(f"warning: could not read the ledger ({exc}); treating it as empty")
        return []
    entries = []
    for c in comments:
        for line in LEDGER_LINE.findall(c.get("body", "")):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("app") == app:
                entries.append(e)
    return entries


def ledger_add(entry: dict) -> None:
    entry = {"ts": int(time.time()), "app": target_app(), **entry}
    try:
        gh_api("POST", f"repos/{REPO}/issues/{ledger_issue()}/comments",
               {"body": "LEDGER " + json.dumps(entry, separators=(",", ":"))})
    except RuntimeError as exc:
        log(f"warning: could not write the ledger ({exc})")


# --- Cloudflare ---------------------------------------------------------------------------

def cf(method: str, path: str, body: dict | None = None) -> dict:
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if not token:
        raise RuntimeError("no Cloudflare token available")
    status, _, text = http("https://api.cloudflare.com/client/v4" + path, method=method,
                           headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                           body=json.dumps(body).encode() if body is not None else None, timeout=30)
    try:
        data = json.loads(text)
    except ValueError:
        data = {}
    if status != 200 or data.get("success") is False:
        msgs = "; ".join(str(e.get("message")) for e in (data.get("errors") or []))[:200]
        raise RuntimeError(f"Cloudflare {method} {path.split('?')[0]} -> {status} {msgs}")
    return data


def waf_rules() -> tuple[str, list[dict]]:
    """(ruleset id, custom firewall rules) for the zone."""
    data = cf("GET", f"/zones/{CF_ZONE}/rulesets/phases/http_request_firewall_custom/entrypoint")
    result = data.get("result") or {}
    rules = [{"id": r.get("id"), "description": r.get("description", ""), "enabled": r.get("enabled", True),
              "action": r.get("action")} for r in result.get("rules") or []]
    return result.get("id", ""), rules


def utc_now() -> float:
    return time.time()


def fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


# --- revisions ------------------------------------------------------------------------------

def _epoch(iso: str | None) -> float:
    if not iso:
        return 0.0
    import calendar
    return float(calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")))


def revisions(app: str | None = None) -> list[dict]:
    rows = az("containerapp", "revision", "list", "-n", app or target_app(), "-g", RG) or []
    out = []
    for r in rows:
        p = r.get("properties") or {}
        containers = ((p.get("template") or {}).get("containers")) or [{}]
        out.append({"name": r.get("name"), "active": bool(p.get("active")), "created": _epoch(p.get("createdTime")),
                    "weight": p.get("trafficWeight"), "health": p.get("healthState"), "running": p.get("runningState"),
                    "replicas": p.get("replicas"), "image": (containers[0] or {}).get("image", "")})
    return sorted(out, key=lambda x: x["created"], reverse=True)
