# D91 (docs/DECISIONS.md) -- runs the three-step "widen the universe" cycle
# from D89's operational plan in sequence: discover new launches, backfill
# their swap history, then replay the grown Store through the online
# policy (safe to re-run repeatedly since D89's mints_already_decided()
# idempotency fix -- it only decides on genuinely new mints).
#
# Meant to be registered as a Scheduled Task on a RECURRING trigger (e.g.
# every 8-10 hours -- must stay under discover_pumpfun_launches.py's own
# ~11h dataset:realtime retention wall, D61), unlike
# run_paper_trade_live_loop.ps1 which is a single long-running process.
# This script runs once per invocation and exits -- Task Scheduler's own
# recurrence handles the "run again later" part, no internal loop needed.
#
# Usage: register as a Scheduled Task action (see docs/DECISIONS.md D91),
# or run directly to test:
#   powershell.exe -ExecutionPolicy Bypass -File <path to this file>

$ErrorActionPreference = "Continue"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -Path $RepoRoot

$VenvPython = Join-Path $RepoRoot "venv\Scripts\python.exe"
if (Test-Path $VenvPython) { $Python = $VenvPython } else { $Python = "python" }

$DataDir = Join-Path $RepoRoot "data"
$ModelState = Join-Path $DataDir "online_policy_state.json"
$LogDir = Join-Path $DataDir "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

$StartedAt = Get-Date -Format "yyyyMMdd_HHmmss"
$LogFile = Join-Path $LogDir "grow_corpus_$StartedAt.log"

function Write-Log($msg) {
    $line = "[$(Get-Date -Format o)] $msg"
    Write-Output $line
    Add-Content -Path $LogFile -Value $line
}

Write-Log "grow_corpus_cycle.ps1 starting -- repo root: $RepoRoot"

Write-Log "step 1/3: discover_pumpfun_launches.py"
& $Python (Join-Path $RepoRoot "scripts\discover_pumpfun_launches.py") --data $DataDir *>> $LogFile
Write-Log "step 1/3 exit code: $LASTEXITCODE"

Write-Log "step 2/3: backfill_discovered_launches.py"
& $Python (Join-Path $RepoRoot "scripts\backfill_discovered_launches.py") `
    --data $DataDir --target-count 300 --limit 50 *>> $LogFile
Write-Log "step 2/3 exit code: $LASTEXITCODE"

Write-Log "step 3/3: paper_trade_replay.py (idempotent re-run, D89 -- only decides on new mints)"
& $Python (Join-Path $RepoRoot "scripts\paper_trade_replay.py") `
    --data $DataDir --model-in $ModelState --model-out $ModelState *>> $LogFile
Write-Log "step 3/3 exit code: $LASTEXITCODE"

Write-Log "grow_corpus_cycle.ps1 done -- see $LogFile for full output"
