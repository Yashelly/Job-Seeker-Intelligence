# Removes the JobSeekerDashboard scheduled task and stops the running dashboard.
param(
    [string]$TaskName = "JobSeekerDashboard"
)

$ErrorActionPreference = "Stop"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    Write-Host "No scheduled task named '$TaskName' found; nothing to remove."
    return
}

try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch {}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed scheduled task '$TaskName'. Any running dashboard from it will stop shortly."
Write-Host "Note: a server process may linger until its port is freed; close it from Task Manager if needed."
