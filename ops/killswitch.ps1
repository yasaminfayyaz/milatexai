<#
    MiLatexAI cost kill-switch — MANUAL EMERGENCY BACKSTOP.

    Day-to-day cost control is now automatic and per-user: the app serves paid
    users + admin always, and free users only while spend < starter + 80% of
    Stripe revenue (see leafbridge/capacity.py). This script is the last-resort
    full stop for a genuine runaway — note it takes EVERYONE down, including
    paying users, so prefer letting the capacity gate do its job.

    A one-command way to instantly stop the hosted server's compute cost if you
    ever get a budget alert. It works by deactivating the app's active revision,
    which drops it to zero replicas (~$0 compute) while LEAVING ingress, the
    milatexai.com custom domain, and the TLS certificate fully intact — so
    reviving is a single command with nothing to re-bind.

    Usage (from this folder):
        .\killswitch.ps1 kill      # disable  -> compute ~$0
        .\killswitch.ps1 revive    # re-enable -> serving again
        .\killswitch.ps1 status    # is it live right now?

    Or just double-click MiLatexAI-KILL.cmd / MiLatexAI-REVIVE.cmd.
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet('kill', 'revive', 'status')]
    [string]$Action = 'status'
)

$ErrorActionPreference = 'Stop'
$App = 'milatexai-app'
$Rg  = 'milatexai-rg'

# Locate the Azure CLI (PATH first, then the known install location).
$az = (Get-Command az -ErrorAction SilentlyContinue).Source
if (-not $az) { $az = 'C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd' }
if (-not (Test-Path $az)) {
    Write-Error "Azure CLI not found. Install it, or edit `$az at the top of this script."
    exit 1
}

function Get-ActiveRevision {
    & $az containerapp revision list -n $App -g $Rg --query "[?properties.active].name | [0]" -o tsv
}
function Get-LatestRevision {
    # ISO-8601 createdTime sorts chronologically, so [-1] is the newest revision.
    & $az containerapp revision list -n $App -g $Rg --query "sort_by([], &properties.createdTime)[-1].name" -o tsv
}

switch ($Action) {
    'kill' {
        $rev = Get-ActiveRevision
        if (-not $rev) { Write-Host "Already DISABLED (no active revision)."; break }
        Write-Host "Disabling MiLatexAI (deactivating $rev)..."
        & $az containerapp revision deactivate -n $App -g $Rg --revision $rev | Out-Null
        Write-Host ""
        Write-Host "  DISABLED. Compute is now ~`$0/hour."
        Write-Host "  Ingress, the milatexai.com domain and the TLS cert are preserved."
        Write-Host "  Bring it back any time with:  .\killswitch.ps1 revive"
    }
    'revive' {
        $rev = Get-LatestRevision
        if (-not $rev) { Write-Error "No revision found to activate."; exit 1 }
        Write-Host "Reviving MiLatexAI (activating $rev)..."
        & $az containerapp revision activate -n $App -g $Rg --revision $rev | Out-Null
        Write-Host ""
        Write-Host "  LIVE again. https://milatexai.com/mcp is back."
        Write-Host "  (The very first request may take a few seconds to cold-start.)"
    }
    'status' {
        $rev = Get-ActiveRevision
        if ($rev) { Write-Host "MiLatexAI is LIVE (active revision: $rev)." }
        else      { Write-Host "MiLatexAI is DISABLED (no active revision)." }
    }
}
