#!/usr/bin/env bash
# Staged, self-verifying production deploy with automatic rollback.
#   1. The new revision starts at 0% traffic.
#   2. It is smoke-tested on its own private revision URL.
#   3. Only then does it take 100% of traffic, and it is smoke-tested again live.
#   4. Any failure puts traffic back on the previous revision and deactivates
#      the new one, so users never stay on a broken build.
# Always-on: every revision is created with min-replicas 1, so one copy is
# running at all times and a request never waits for a ~30 second cold start.
# While that copy is quiet, Azure bills it at the cheap idle rate. Because a
# min-replicas-1 revision keeps its copy running even at 0% traffic, a
# successful deploy deactivates EVERY other revision; keeping the previous one
# warm as a standby would double the bill.
#
#   Usage: ops/deploy.sh <git-sha>     (the image ghcr.io/...:<sha> must exist)
# Manual redeploy or rollback: run the "deploy" workflow with an older commit
# SHA (the images stay in the registry; this takes about 2 minutes), or this
# script locally after `az login`. Do NOT use a bare `az containerapp update`
# any more: the app runs in multiple-revision mode, so a bare update creates a
# revision that receives no traffic.
set -euo pipefail
SHA="${1:?usage: deploy.sh <git-sha>}"
# DEPLOY_APP lets the incident drills rehearse a rollback on a throwaway copy
# (milatexai-drill). Anything else is refused, so this can only touch those two.
APP="${DEPLOY_APP:-milatexai-app}"
case "$APP" in milatexai-app|milatexai-drill) ;; *) echo "refusing to deploy to unknown app: $APP" >&2; exit 1;; esac
MIN_REPLICAS=1; [ "$APP" = "milatexai-app" ] || MIN_REPLICAS=0   # the drill copy stays scale-to-zero (no idle bill)
# Scaling: one copy always runs; more are added up to MAX_REPLICAS when ANY rule fires:
#   CPU_PCT    average CPU across copies above this percentage (the main signal: compiles)
#   MEM_PCT    average memory above this percentage (large papers compile with a lot of memory)
#   FLOOD_RPS  more than this many requests per second per copy (Azure averages over 15 s);
#              only a backstop for floods of light requests, which wait on Git and use no CPU
# Extra copies are removed after 300 quiet seconds (Azure's fixed scale-down window). A copy
# being removed gets GRACE seconds to finish its requests (uvicorn drains; see the Dockerfile).
# Every copy must be interchangeable: shared state lives in storage (see store.py).
MAX_REPLICAS="${DEPLOY_MAX_REPLICAS:-5}"; [ "$APP" = "milatexai-app" ] || MAX_REPLICAS="${DEPLOY_MAX_REPLICAS:-3}"
CPU_PCT=70
MEM_PCT=80
FLOOD_RPS=20
COOLDOWN=300
GRACE=120
RG=milatexai-rg
IMAGE="ghcr.io/yasaminfayyaz/milatexai:$SHA"
HERE="$(cd "$(dirname "$0")" && pwd)"
export MSYS_NO_PATHCONV=1   # Git Bash on Windows: don't mangle /subscriptions/... ids
az config set extension.use_dynamic_install=yes_without_prompt --only-show-errors >/dev/null 2>&1 || true
# Every Azure call gets a hard 10 minute limit, so one hung call cannot stall a deploy.
azq() { timeout 600 az "$@" --only-show-errors; }
# Azure refuses a change while the previous one is still being applied, so wait for it.
settle() {
  local st=""
  for _ in $(seq 1 60); do
    st=$(azq containerapp show -n "$APP" -g "$RG" --query properties.provisioningState -o tsv || true)
    case "$st" in Succeeded|Failed|Canceled) return 0;; esac
    sleep 5
  done
  echo "::warning::$APP still provisioning after 5 minutes (state: $st)"
}

# Revision currently serving traffic. Single-revision mode reports only a
# "latestRevision" entry, so fall back to the latest ready revision.
PREV=$(azq containerapp ingress traffic show -n "$APP" -g "$RG" \
  --query "[?weight==\`100\`] | [0].revisionName" -o tsv || true)
if [ -z "$PREV" ] || [ "$PREV" = "None" ]; then
  PREV=$(azq containerapp show -n "$APP" -g "$RG" --query properties.latestReadyRevisionName -o tsv)
fi
echo "previous (live) revision: $PREV"

# Multiple-revision mode with traffic pinned BY NAME, so a new revision starts at 0%.
azq containerapp revision set-mode -n "$APP" -g "$RG" --mode multiple >/dev/null
azq containerapp ingress traffic set -n "$APP" -g "$RG" --revision-weight "$PREV=100" >/dev/null

SUFFIX="g${SHA:0:8}-r${GITHUB_RUN_NUMBER:-0}-${GITHUB_RUN_ATTEMPT:-$(date +%s)}"
NEW="$APP--$SUFFIX"
# The CLI can set only one scale rule, so the three rules (and the scale-in delay) are set on
# the app template through the API, and every later `containerapp update` copies them into
# its new revision. The applied set is remembered in the app's `scale_policy` tag, so this
# runs only when the policy above changes. The patch needs a fresh revision name (Azure
# rejects a duplicate) and creates a throwaway revision with no traffic, switched off at once.
POLICY="cpu${CPU_PCT}-mem${MEM_PCT}-req${FLOOD_RPS}-cd${COOLDOWN}-min${MIN_REPLICAS}-max${MAX_REPLICAS}"
CUR_POLICY=$(azq containerapp show -n "$APP" -g "$RG" --query "tags.scale_policy" -o tsv || true)
if [ "$CUR_POLICY" != "$POLICY" ]; then
  APP_ID_FOR_PATCH=$(azq containerapp show -n "$APP" -g "$RG" --query id -o tsv)
  SCALE_BODY=$(printf '{"properties":{"template":{"revisionSuffix":"scale%s","scale":{"minReplicas":%s,"maxReplicas":%s,"cooldownPeriod":%s,"pollingInterval":30,"rules":[{"name":"cpu-busy","custom":{"type":"cpu","metadata":{"type":"Utilization","value":"%s"}}},{"name":"memory-busy","custom":{"type":"memory","metadata":{"type":"Utilization","value":"%s"}}},{"name":"request-flood","http":{"metadata":{"concurrentRequests":"%s"}}}]}}}}' \
    "$(date +%s)" "$MIN_REPLICAS" "$MAX_REPLICAS" "$COOLDOWN" "$CPU_PCT" "$MEM_PCT" "$FLOOD_RPS")
  azq rest --method patch --url "https://management.azure.com${APP_ID_FOR_PATCH}?api-version=2025-01-01" --body "$SCALE_BODY" >/dev/null
  for _ in $(seq 1 36); do
    PATCHED=$(azq containerapp show -n "$APP" -g "$RG" --query properties.latestRevisionName -o tsv)
    [ "$PATCHED" != "$PREV" ] && break
    sleep 5
  done
  settle
  if [ "$PATCHED" != "$PREV" ]; then
    azq containerapp ingress traffic set -n "$APP" -g "$RG" --revision-weight "$PREV=100" >/dev/null
    azq containerapp revision deactivate -n "$APP" -g "$RG" --revision "$PATCHED" >/dev/null || true
  fi
  settle
  azq tag update --resource-id "$APP_ID_FOR_PATCH" --operation Merge --tags scale_policy="$POLICY" >/dev/null
  echo "scale policy set: $POLICY"
fi

# Sign-in (WorkOS). Accounts were created in the WorkOS STAGING environment; the PRODUCTION
# environment runs alongside it. SIGNIN picks the one new sign-ins use (Claude, ChatGPT and the
# website); the other stays trusted, so existing connections keep working, and a production
# sign-in is linked to the existing account by verified email (hosted.workos_environments).
# Production's API key is the app secret workos-prod-key; an app without it (the drill copy)
# gets none of these settings. Rolling back the switch = SIGNIN=staging and push.
SIGNIN=staging
WORKOS_PROD_DOMAIN=https://discerning-tranquility-74.authkit.app
WORKOS_PROD_CLIENT=client_01KX9FBJGB3T5MG7EPYH59KTEA
SIGNIN_VARS=()
if [ "$(azq containerapp secret list -n "$APP" -g "$RG" --query "length([?name=='workos-prod-key'])" -o tsv || echo 0)" = "1" ]; then
  SIGNIN_VARS=(WORKOS_PROD_AUTHKIT_DOMAIN="$WORKOS_PROD_DOMAIN" WORKOS_PROD_CLIENT_ID="$WORKOS_PROD_CLIENT"
               WORKOS_PROD_API_KEY=secretref:workos-prod-key WORKOS_SIGNIN="$SIGNIN")
  echo "sign-in: new sign-ins use $SIGNIN, both WorkOS environments trusted"
fi

# FREE_CAPACITY_STARTER: month-to-date Azure spend (CAD) at which the capacity
# gate pauses the FREE tier (plus 80% of Pro revenue, see leafbridge/capacity.py).
# Pro is never paused. Kept here so the limit is reviewable in git.
azq containerapp update -n "$APP" -g "$RG" --image "$IMAGE" --revision-suffix "$SUFFIX" \
  --min-replicas "$MIN_REPLICAS" --max-replicas "$MAX_REPLICAS" \
  --termination-grace-period "$GRACE" \
  --set-env-vars FREE_CAPACITY_STARTER=100 ${SIGNIN_VARS[@]+"${SIGNIN_VARS[@]}"} >/dev/null
echo "new revision: $NEW (0% traffic)"

rollback() {
  echo "ROLLING BACK: traffic -> $PREV, deactivating $NEW" >&2
  azq containerapp ingress traffic set -n "$APP" -g "$RG" --revision-weight "$PREV=100" >/dev/null || true
  azq containerapp revision deactivate -n "$APP" -g "$RG" --revision "$NEW" >/dev/null || true
  exit 1
}

state=""
for _ in $(seq 1 72); do
  state=$(azq containerapp revision show -n "$APP" -g "$RG" --revision "$NEW" \
    --query properties.provisioningState -o tsv || true)
  [ "$state" = "Provisioned" ] && break
  [ "$state" = "Failed" ] && { echo "revision failed to provision" >&2; rollback; }
  sleep 5
done
[ "$state" = "Provisioned" ] || { echo "revision not provisioned in time (state: $state)" >&2; rollback; }

NEW_FQDN=$(azq containerapp revision show -n "$APP" -g "$RG" --revision "$NEW" --query properties.fqdn -o tsv)
echo "--- smoke test on the new revision's private URL ---"
bash "$HERE/smoke.sh" "https://$NEW_FQDN" || rollback

azq containerapp ingress traffic set -n "$APP" -g "$RG" --revision-weight "$NEW=100" >/dev/null
echo "traffic -> $NEW"
APP_FQDN=$(azq containerapp show -n "$APP" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)
echo "--- smoke test on the live URL ---"
bash "$HERE/smoke.sh" "https://$APP_FQDN" || rollback

# Remember which version is live and which one it replaced, only now that both smoke
# tests passed. The incident repair reads these tags to find a known-good version to
# roll back to. Redeploying the previous version clears "previous" so a second rollback
# can never land on the version that was just rolled away from.
APP_ID=$(azq containerapp show -n "$APP" -g "$RG" --query id -o tsv)
OLD_CUR=$(azq containerapp show -n "$APP" -g "$RG" --query "tags.current_sha" -o tsv 2>/dev/null || true)
OLD_PREV=$(azq containerapp show -n "$APP" -g "$RG" --query "tags.previous_sha" -o tsv 2>/dev/null || true)
[ "$OLD_CUR" = "None" ] && OLD_CUR=""; [ "$OLD_PREV" = "None" ] && OLD_PREV=""
if [ "$OLD_CUR" = "$SHA" ]; then NEW_PREV="$OLD_PREV"
elif [ "$OLD_PREV" = "$SHA" ]; then NEW_PREV=""
else NEW_PREV="$OLD_CUR"; fi
azq tag update --resource-id "$APP_ID" --operation Merge \
  --tags current_sha="$SHA" previous_sha="$NEW_PREV" deployed_at="$(date +%s)" >/dev/null \
  || echo "::warning::could not record the deployed version (automatic rollback will have no target)"

echo "letting in-flight requests on the old revision finish (60s)..."
sleep 60
for r in $(azq containerapp revision list -n "$APP" -g "$RG" --query "[?properties.active].name" -o tsv); do
  if [ "$r" != "$NEW" ]; then
    if azq containerapp revision deactivate -n "$APP" -g "$RG" --revision "$r" >/dev/null; then
      echo "deactivated old revision $r"
    else
      echo "::warning::could not deactivate $r; it may keep a warm copy running and add cost"
    fi
  fi
done
echo "DEPLOYED $SHA as $NEW (to roll back, redeploy an older commit SHA; previous was $PREV)"
