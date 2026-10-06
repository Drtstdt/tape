# D91 (docs/DECISIONS.md) -- keeps scripts/paper_trade_live.py running
# unattended: auto-restarts it if it ever exits (crash, an uncaught
# exception, a transient network failure that bubbles past its own 429
# retry logic), and is meant to be registered as a Windows Scheduled Task
# that fires at log-on/startup, so the whole thing survives a reboot too.
#
# WHAT THIS DOES NOT DO (a deliberate scope decision, not an oversight --
# see D91): it does NOT preserve paper_trade_live.py's in-flight
# TrackedMint state (tokens that were discovered but not yet decided, or
# decided but not yet resolved) across a restart. That state lives in
# TokenState/BarBuilder objects with deques and nested dataclasses that
# would be real work to serialize correctly, and a subtly wrong
# serialization would silently corrupt running state -- a worse failure
# mode than the bounded, honest cost of just losing it. The cost is
# bounded by --max-concurrent (15 by default): a restart loses at most 15
# tokens' worth of in-progress tracking, a small fraction of a single
# day's decision volume. The online_policy_state.json MODEL checkpoint
# (the part that actually matters for learning) IS correctly persisted and
# reloaded across every restart, by paper_trade_live.py itself -- this
# wrapper doesn't touch that, it relies on it.
#
# Usage: register this file as a Scheduled Task's action (see the setup
# notes in docs/DECISIONS.md D91, or just:
#   powershell.exe -ExecutionPolicy Bypass -File <path to this file>
# ), or run it directly in a terminal to test it first.

$ErrorActionPreference = "Continue"

# Repo root is this script's parent directory's parent (scripts/.. == tape/).
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -Path $RepoRoot

# Prefer the project's own venv if present; fall back to whatever `python`
# resolves to on PATH otherwise (e.g. if this is run from an already-active
# venv shell).
$VenvPython = Join-Path $RepoRoot "venv\Scripts\python.exe"
if (Test-Path $VenvPython) {
    $Python = $VenvPython
} else {
    $Python = "python"
}

$DataDir = Join-Path $RepoRoot "data"
$ModelState = Join-Path $DataDir "online_policy_state.json"
$LogDir = Join-Path $DataDir "logs"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

Write-Output "[$(Get-Date -Format o)] run_paper_trade_live_loop.ps1 starting -- repo root: $RepoRoot"
Write-Output "[$(Get-Date -Format o)] using python: $Python"

while ($true) {
    $StartedAt = Get-Date -Format "yyyyMMdd_HHmmss"
    $LogFile = Join-Path $LogDir "paper_trade_live_$StartedAt.log"
    Write-Output "[$(Get-Date -Format o)] launching paper_trade_live.py -- log: $LogFile"

    & $Python (Join-Path $RepoRoot "scripts\paper_trade_live.py") `
        --data $DataDir `
        --model-in $ModelState `
        --model-out $ModelState `
        *>> $LogFile

    $ExitCode = $LASTEXITCODE
    Write-Output "[$(Get-Date -Format o)] paper_trade_live.py exited (code $ExitCode) -- see $LogFile"
    Write-Output "[$(Get-Date -Format o)] restarting in 30s (Ctrl+C now to stop the loop entirely)"
    Start-Sleep -Seconds 30
}
