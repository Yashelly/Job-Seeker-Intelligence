# Registers a Windows Scheduled Task that keeps the Job Seeker web dashboard
# running in the background on this PC. It starts at logon, restarts on failure,
# and runs as the current user (so the claude/codex CLI login is available).
#
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\install_dashboard_service.ps1 [-Port 8000]
# Remove: powershell -ExecutionPolicy Bypass -File scripts\uninstall_dashboard_service.ps1
param(
    [int]$Port = 8000,
    [string]$TaskName = "JobSeekerDashboard"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $root "scripts\run_dashboard.ps1"
if (-not (Test-Path $runner)) {
    throw "Launcher not found: $runner"
}

$argument = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`" -Port $Port"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName

Write-Host "Installed scheduled task '$TaskName'."
Write-Host "The dashboard now starts at logon and is running at http://127.0.0.1:$Port/"
Write-Host "Logs: $(Join-Path $root 'logs\dashboard.log')"
Write-Host "Remove it with: powershell -ExecutionPolicy Bypass -File scripts\uninstall_dashboard_service.ps1"
