<#
.SYNOPSIS
    Seed a throwaway demo database from bundled fixtures and open the dashboard.

.DESCRIPTION
    Runs the offline demo path: local sample fixtures + deterministic rule-based
    scoring, so no network, API key, or CLI login is required. Writes only under
    demo_data/ and re-seeds on every run. Must be run from the project root.

.PARAMETER Port
    Loopback port for the dashboard (default 8000).

.PARAMETER NoWeb
    Only seed the demo database; do not open the dashboard.
#>
param(
    [int]$Port = 8000,
    [switch]$NoWeb
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = if (Test-Path "$root\.venv\Scripts\python.exe") { "$root\.venv\Scripts\python.exe" } else { "python" }

$demoArgs = @("main.py", "--demo")
if (-not $NoWeb) { $demoArgs += @("--web", "--web-port", "$Port") }

& $python @demoArgs
