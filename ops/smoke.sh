#!/usr/bin/env bash
# Post-deploy smoke test against one deployed base URL (a revision's private
# URL, or the live app URL). Exits non-zero on the first failure, which makes
# ops/deploy.sh roll traffic back.
#   Usage: ops/smoke.sh https://<host>
set -euo pipefail
BASE="${1:?usage: smoke.sh https://host}"
PY="${PYTHON:-python3}"
fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }

# The revision may be scaled to zero: allow a cold start (up to ~3 minutes).
code=000
for _ in $(seq 1 36); do
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 20 "$BASE/health/capacity" || true)
  [ "$code" = "200" ] && break
  sleep 5
done
[ "$code" = "200" ] || fail "/health/capacity never returned 200 (last: $code)"

health=$(curl -sf --max-time 30 "$BASE/health/capacity")
echo "$health" | "$PY" -c '
import json, sys
h = json.load(sys.stdin)
assert h["latex_available"] is True, "LaTeX engine unavailable"
assert h["figure_studio"] is True, "Figure Studio unavailable"
' || fail "health degraded: $health"
echo "ok  GET /health/capacity (LaTeX and Figure Studio available)"

check() {  # path expected_status [text_that_must_appear]
  local path=$1 want=$2 needle=${3:-} out status body
  out=$(curl -s --max-time 30 -w $'\n%{http_code}' "$BASE$path" || true)
  status=${out##*$'\n'}
  body=${out%$'\n'*}
  [ "$status" = "$want" ] || fail "GET $path -> $status (want $want)"
  if [ -n "$needle" ] && ! grep -q "$needle" <<<"$body"; then
    fail "GET $path is missing '$needle'"
  fi
  echo "ok  GET $path $status"
}
check / 200 MiLatexAI
check /tools 200
check /tools/bibtex 200
check /tools/latex-error-finder 200
check /.well-known/oauth-protected-resource/mcp 200
check /.well-known/oauth-authorization-server 200

# The MCP endpoint is alive AND still demands OAuth (never open to anonymous use).
hdrs=$(curl -s -D - -o /dev/null --max-time 30 -X POST "$BASE/mcp" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' || true)
grep -qE "^HTTP/[0-9.]+ 401" <<<"$hdrs" || fail "POST /mcp without auth did not return 401"
grep -qi "www-authenticate: Bearer resource_metadata" <<<"$hdrs" || fail "POST /mcp 401 lacks the OAuth challenge"
echo "ok  POST /mcp -> 401 with OAuth challenge"

echo "SMOKE PASS: $BASE"
