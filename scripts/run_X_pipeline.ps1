# D142: the whole family-X pipeline, one step after another; stops at the first failure.
#   powershell -ExecutionPolicy Bypass -File scripts\run_X_pipeline.ps1
# Each step writes its own log with ETA (see the paths printed by each script).
$ErrorActionPreference = "Stop"
$steps = @(
    @("build_paths discovery",            "scripts\build_paths.py", "--zone", "discovery", "--workers", "6"),
    @("search_X (up to 24 h)",            "scripts\search_X.py", "--workers", "6", "--hours", "24"),
    @("research set validation",          "scripts\build_research_set_v2.py", "--zone", "validation", "--workers", "6"),
    @("build_paths validation",           "scripts\build_paths.py", "--zone", "validation", "--workers", "6"),
    @("validate_X (the one look)",        "scripts\validate_X.py")
)
$t0 = Get-Date
$i = 0
foreach ($s in $steps) {
    $i++
    $name = $s[0]
    $args_ = $s[1..($s.Length - 1)]
    Write-Host ""
    Write-Host ("=== [{0}/{1}] {2}  started {3:yyyy-MM-dd HH:mm:ss}  (pipeline elapsed {4:N1} h) ===" -f $i, $steps.Count, $name, (Get-Date), ((Get-Date) - $t0).TotalHours)
    & python @args_
    if ($LASTEXITCODE -ne 0) {
        Write-Host ("STOPPED: step '{0}' exited with code {1} -- later steps not run." -f $name, $LASTEXITCODE)
        exit $LASTEXITCODE
    }
}
Write-Host ("=== pipeline done in {0:N1} h ===" -f ((Get-Date) - $t0).TotalHours)
