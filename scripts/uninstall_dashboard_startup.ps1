# Removes the per-user autostart entry created by install_dashboard_startup.ps1.
# Does not require admin. Any dashboard already running keeps running until you
# close it (Ctrl+C in its window, or stop the python.exe from Task Manager).
$ErrorActionPreference = "Stop"

$startup = [Environment]::GetFolderPath('Startup')
$vbsPath = Join-Path $startup "JobSeekerDashboard.vbs"

if (Test-Path $vbsPath) {
    Remove-Item $vbsPath -Force
    Write-Host "Removed autostart entry: $vbsPath"
} else {
    Write-Host "No autostart entry found at $vbsPath; nothing to remove."
}
Write-Host "Note: a dashboard already running is not stopped by this. Close it manually if needed."
