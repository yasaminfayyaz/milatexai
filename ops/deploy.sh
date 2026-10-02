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
APP=milatexai-app
RG=milatexai-rg
IMAGE="ghcr.io/yasaminfayyaz/milatexai:$SHA"
HERE="$(cd "$(dirname "$0")" && pwd)"
export MSYS_NO_PATHCONV=1   # Git Bash on Windows: don't mangle /subscriptions/... ids
az config set extension.use_dynamic_install=yes_without_prompt --only-show-errors >/dev/null 2>&1 || true
azq() { az "$@" --only-show-errors; }

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
# FREE_CAPACITY_STARTER: month-to-date Azure spend (CAD) at which the capacity
# gate pauses the FREE tier (plus 80% of Pro revenue, see leafbridge/capacity.py).
# Pro is never paused. Kept here so the limit is reviewable in git.
azq containerapp update -n "$APP" -g "$RG" --image "$IMAGE" --revision-suffix "$SUFFIX" \
  --min-replicas 1 --set-env-vars FREE_CAPACITY_STARTER=100 >/dev/null
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
