param(
    [ValidatePattern("^(?:[01]\d|2[0-3]):[0-5]\d$")]
    [string]$At = "09:00",
    [string]$TaskName = "JobSeekerDaily",
    [ValidateRange(5, 300)]
    [int]$ResourceSampleSeconds = 30
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $PSScriptRoot "run_daily.ps1"
$python = (Get-Command python -ErrorAction Stop).Source
$powerShell = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
if (-not $powerShell) {
    $powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
}

$runAt = [datetime]::ParseExact(
    $At,
    "HH:mm",
    [System.Globalization.CultureInfo]::InvariantCulture
)
$actionArguments = (
    "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass " +
    "-File `"$runner`" -PythonExecutable `"$python`" " +
    "-ResourceSampleSeconds $ResourceSampleSeconds"
)
$action = New-ScheduledTaskAction `
    -Execute $powerShell `
    -Argument $actionArguments `
    -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $runAt
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 12)
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Daily Job Seeker search and Telegram summary." `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

Write-Host "Scheduled task '$TaskName' installed."
Write-Host "Runs daily at $At while this Windows user is signed in."
Write-Host "Project: $projectRoot"
Write-Host "Python:  $python"
Write-Host "Log:     $(Join-Path $projectRoot 'logs\daily.log')"
Write-Host "Metrics: $(Join-Path $projectRoot 'logs\resources.log')"
