# Watchdog and automatic repair

Two parts, on two different companies' infrastructure on purpose.

| Part | Runs on | What it does |
| --- | --- | --- |
| Watchdog (`ops/watchdog/worker.js`) | Cloudflare Worker, every minute | Checks the site, the connector and the app's own health from outside Azure. Serves https://status.milatexai.com. Emails the owner. Starts the repair workflow. |
| Repair (`.github/workflows/incident.yml`, `ops/incident/`) | GitHub Actions | First aid, evidence, AI triage, a gated executor, then a report. |

A third piece, `watchdog-backstop.yml`, checks that the watchdog itself is still alive.

## The safety rules (all in `plan.py`, all tested)

* The AI only proposes. It runs in a job with no cloud credentials and read-only tools.
* The proposal must be one of: `restart`, `rollback`, `purge_cache`, `disable_waf_rule`, `escalate`, `none`.
* Each action has preconditions. Examples: no restart when the app answers directly; rollback only
  to the version recorded in the container app's `previous_sha` tag and only within 6 hours of a deploy;
  a firewall rule is only ever switched off if its description starts with `[auto-disableable]`.
* Limits: 3 automatic changes per hour, 1 rollback per day, nothing while a deploy is running, no
  second restart for the same incident.
* Anything refused or unclear becomes an email that says "Needs you".

## Kill switch

Automatic changes to production only happen while the repository variable `AUTOFIX_ENABLED` is
exactly `true`. Anything else turns every action into a recorded dry run (the owner is still emailed).

```bash
gh variable set AUTOFIX_ENABLED --body false --repo yasaminfayyaz/milatexai   # stop
gh variable set AUTOFIX_ENABLED --body true --repo yasaminfayyaz/milatexai    # allow
```

## Rehearse

`milatexai-drill` is a throwaway copy of the app (scale to zero, no secrets). Break it, then start the
repair against it:

```bash
az containerapp update -n milatexai-drill -g milatexai-rg --image mcr.microsoft.com/k8se/quickstart:latest --revision-suffix drillbad
az containerapp ingress traffic set -n milatexai-drill -g milatexai-rg --revision-weight milatexai-drill--drillbad=100
gh workflow run incident.yml -f incident_id=drill-1 -f kind=drill -f failing=origin -f drill_app=milatexai-drill
```

## Where things live

* Secrets (GitHub environments `incident-fix` and `watchdog-deploy`): `CLOUDFLARE_API_TOKEN`,
  `WATCHDOG_GITHUB_TOKEN`, `WATCHDOG_REPORT_SECRET`. Repository secret `INCIDENT_EVIDENCE_KEY` encrypts
  the evidence and the AI's decision, because this repository is public.
* Email goes through the Worker (Cloudflare Email Service), from alerts@milatexai.com.
* The ledger of automatic changes is the issue labelled `incident-ledger`.
* Re-publish the Worker by hand: `python ops/watchdog/deploy.py` (needs the three secrets as environment variables).
