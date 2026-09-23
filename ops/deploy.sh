#!/usr/bin/env bash
# Staged, self-verifying production deploy with automatic rollback.
#   1. The new revision starts at 0% traffic.
#   2. It is smoke-tested on its own private revision URL.
#   3. Only then does it take 100% of traffic, and it is smoke-tested again live.
#   4. Any failure puts traffic back on the previous revision and deactivates
#      the new one, so users never stay on a broken build.
# The previous revision is kept active (scaled to zero, no cost) for instant
# manual rollback; anything older is deactivated.
#
#   Usage: ops/deploy.sh <git-sha>     (the image ghcr.io/...:<sha> must exist)
# Manual redeploy or rollback: run the "deploy" workflow with a SHA, or this
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
azq containerapp update -n "$APP" -g "$RG" --image "$IMAGE" --revision-suffix "$SUFFIX" >/dev/null
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

for r in $(azq containerapp revision list -n "$APP" -g "$RG" --query "[?properties.active].name" -o tsv); do
  if [ "$r" != "$NEW" ] && [ "$r" != "$PREV" ]; then
    azq containerapp revision deactivate -n "$APP" -g "$RG" --revision "$r" >/dev/null && echo "deactivated old revision $r"
  fi
done
echo "DEPLOYED $SHA as $NEW (rollback target kept: $PREV)"
