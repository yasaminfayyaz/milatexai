#!/usr/bin/env python3
"""Deploy the watchdog Worker to Cloudflare. Safe to run again and again.

It makes sure that, in this order:
  1. the account has a workers.dev subdomain (Cloudflare needs one before a Worker can run on a schedule),
  2. the KV namespace that holds the watchdog's memory exists,
  3. the Worker code is uploaded with its bindings (KV, outgoing email, two secrets),
  4. it runs every minute,
  5. it answers on status.milatexai.com and nowhere else (the workers.dev address is switched off).

Reads these from the environment (the GitHub environment "watchdog-deploy" provides them):
  CLOUDFLARE_API_TOKEN, WATCHDOG_GITHUB_TOKEN, WATCHDOG_REPORT_SECRET
Nothing secret is ever printed.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ACCOUNT = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "ee666a4f2c1b7d4a6c75dbbd54ef9e5a")
ZONE = os.environ.get("CLOUDFLARE_ZONE_ID", "1378686c9478bd4880bca1288ead3079")
SCRIPT = "milatexai-watchdog"
KV_TITLE = "milatexai-watchdog"
SUBDOMAIN = "milatexai"
HOST = "status.milatexai.com"
CRON = "* * * * *"
COMPAT_DATE = "2025-09-01"
CODE = Path(__file__).with_name("worker.js")


def need(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"missing {name}")
    return value


TOKEN = need("CLOUDFLARE_API_TOKEN")


def call(method: str, path: str, body=None, raw: bytes | None = None, content_type: str | None = None,
         ok_codes: tuple[int, ...] = (200,), allow_failure: bool = False) -> dict:
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request("https://api.cloudflare.com/client/v4" + path, method=method, data=data)
    req.add_header("Authorization", "Bearer " + TOKEN)
    if data is not None:
        req.add_header("Content-Type", content_type or "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            status, payload = resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            payload = json.loads(exc.read() or b"{}")
        except ValueError:
            payload = {}
    if allow_failure and status in ok_codes:
        return payload
    if status not in ok_codes or payload.get("success") is False:
        errors = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in payload.get("errors") or []) or "no detail"
        sys.exit(f"{method} {path} failed ({status}): {errors}")
    return payload


def ensure_subdomain() -> None:
    got = call("GET", f"/accounts/{ACCOUNT}/workers/subdomain", ok_codes=(200, 404), allow_failure=True)
    if (got.get("result") or {}).get("subdomain"):
        print(f"ok  workers.dev subdomain: {got['result']['subdomain']}")
        return
    call("PUT", f"/accounts/{ACCOUNT}/workers/subdomain", {"subdomain": SUBDOMAIN})
    print(f"set workers.dev subdomain: {SUBDOMAIN}")


def ensure_kv() -> str:
    page = call("GET", f"/accounts/{ACCOUNT}/storage/kv/namespaces?per_page=100")
    for ns in page.get("result") or []:
        if ns.get("title") == KV_TITLE:
            print("ok  KV namespace exists")
            return ns["id"]
    made = call("POST", f"/accounts/{ACCOUNT}/storage/kv/namespaces", {"title": KV_TITLE})
    print("made KV namespace")
    return made["result"]["id"]


def upload(kv_id: str) -> None:
    metadata = {
        "main_module": "worker.js",
        "compatibility_date": COMPAT_DATE,
        "observability": {"enabled": True, "head_sampling_rate": 1},
        "bindings": [
            {"type": "kv_namespace", "name": "STATE", "namespace_id": kv_id},
            {"type": "send_email", "name": "EMAIL"},
            {"type": "secret_text", "name": "GITHUB_TOKEN", "text": need("WATCHDOG_GITHUB_TOKEN")},
            {"type": "secret_text", "name": "REPORT_SECRET", "text": need("WATCHDOG_REPORT_SECRET")},
        ],
    }
    boundary = uuid.uuid4().hex
    parts = [
        (f'form-data; name="metadata"', "application/json", json.dumps(metadata).encode()),
        (f'form-data; name="worker.js"; filename="worker.js"', "application/javascript+module", CODE.read_bytes()),
    ]
    raw = b""
    for disposition, ctype, content in parts:
        raw += (f"--{boundary}\r\nContent-Disposition: {disposition}\r\nContent-Type: {ctype}\r\n\r\n").encode()
        raw += content + b"\r\n"
    raw += f"--{boundary}--\r\n".encode()
    call("PUT", f"/accounts/{ACCOUNT}/workers/scripts/{SCRIPT}", raw=raw,
         content_type=f"multipart/form-data; boundary={boundary}")
    print(f"ok  uploaded {CODE.name} ({len(CODE.read_bytes())} bytes)")


def set_schedule() -> None:
    call("PUT", f"/accounts/{ACCOUNT}/workers/scripts/{SCRIPT}/schedules", [{"cron": CRON}])
    print(f"ok  runs on schedule: {CRON}")


def attach_domain() -> None:
    call("PUT", f"/accounts/{ACCOUNT}/workers/domains",
         {"environment": "production", "hostname": HOST, "service": SCRIPT, "zone_id": ZONE})
    print(f"ok  answers on https://{HOST}")


def close_workers_dev() -> None:
    call("POST", f"/accounts/{ACCOUNT}/workers/scripts/{SCRIPT}/subdomain", {"enabled": False, "previews_enabled": False})
    print("ok  workers.dev address switched off")


def main() -> None:
    ensure_subdomain()
    kv_id = ensure_kv()
    upload(kv_id)
    set_schedule()
    attach_domain()
    close_workers_dev()
    print("DEPLOYED")


if __name__ == "__main__":
    main()
