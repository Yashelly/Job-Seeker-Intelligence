# Launches the Job Seeker local web dashboard, logging to logs/dashboard.log.
# Used by the "JobSeekerDashboard" scheduled task, but can also be run by hand.
param(
    [int]$Port = 8000,
    # Leave empty to use the database resolved from config (config/job_seeker.db),
    # matching a plain `python main.py --web`. Pass a path only to override it.
    [string]$DbPath = ""
)

$ErrorActionPreference = "Stop"

# Project root is the parent of this scripts/ directory.
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}
$log = Join-Path $logDir "dashboard.log"

# Resolve a Python interpreter from PATH.
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    $python = (Get-Command python3 -ErrorAction SilentlyContinue).Source
}
if (-not $python) {
    "[{0}] ERROR: python not found on PATH." -f (Get-Date -Format s) | Out-File -Append -Encoding utf8 $log
    exit 1
}

$dbLabel = if ($DbPath) { $DbPath } else { "config (default)" }
"[{0}] Starting dashboard on http://127.0.0.1:{1}/ (db={2})" -f (Get-Date -Format s), $Port, $dbLabel |
    Out-File -Append -Encoding utf8 $log

# Build args; only pass --db when explicitly overridden, so the app resolves the
# same config database as a plain `python main.py --web`.
$pyArgs = @("main.py", "--web", "--web-host", "127.0.0.1", "--web-port", "$Port")
if ($DbPath) { $pyArgs += @("--db", $DbPath) }

# uvicorn writes its startup/access logs to stderr. Under `*>> $log` with
# $ErrorActionPreference = "Stop", the first stderr line is promoted to a
# terminating NativeCommandError that kills python before it binds the port.
# Relax to "Continue" for the long-running server so its stderr is just logged.
$ErrorActionPreference = "Continue"
& $python @pyArgs *>> $log
