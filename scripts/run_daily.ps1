param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExecutable,
    [ValidateRange(5, 300)]
    [int]$ResourceSampleSeconds = 30
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$mainScript = Join-Path $projectRoot "main.py"
$logDirectory = Join-Path $projectRoot "logs"
$logPath = Join-Path $logDirectory "daily.log"
$resourceLogPath = Join-Path $logDirectory "resources.log"
$previousLogPath = Join-Path $logDirectory "daily.previous.log"
$previousResourceLogPath = Join-Path $logDirectory "resources.previous.log"
$stdoutPath = Join-Path $logDirectory "daily.stdout.tmp"
$stderrPath = Join-Path $logDirectory "daily.stderr.tmp"

function Rotate-LogFile {
    param(
        [string]$Path,
        [string]$PreviousPath
    )

    if ((Test-Path -LiteralPath $Path) -and (Get-Item -LiteralPath $Path).Length -gt 5MB) {
        Move-Item -LiteralPath $Path -Destination $PreviousPath -Force
    }
}

function Copy-NewLogLines {
    param(
        [string]$SourcePath,
        [string]$DestinationPath,
        [ref]$CopiedLineCount
    )

    try {
        if (-not (Test-Path -LiteralPath $SourcePath)) {
            return
        }

        $lines = @(Get-Content -LiteralPath $SourcePath)
        if ($lines.Count -le $CopiedLineCount.Value) {
            return
        }

        $newLines = if ($CopiedLineCount.Value -eq 0) {
            $lines
        }
        else {
            $lines[$CopiedLineCount.Value..($lines.Count - 1)]
        }
        Add-Content -LiteralPath $DestinationPath -Encoding utf8 -Value $newLines
        $CopiedLineCount.Value = $lines.Count
    }
    catch {
        return
    }
}

function Get-ProcessTreeSnapshot {
    param([int]$RootProcessId)

    $allProcesses = @(
        Get-CimInstance Win32_Process -Property `
            ProcessId,ParentProcessId,Name,WorkingSetSize,KernelModeTime,UserModeTime
    )
    $processIds = [System.Collections.Generic.HashSet[int]]::new()
    [void]$processIds.Add($RootProcessId)

    do {
        $added = $false
        foreach ($item in $allProcesses) {
            $parentId = [int]$item.ParentProcessId
            $processId = [int]$item.ProcessId
            if ($processIds.Contains($parentId) -and $processIds.Add($processId)) {
                $added = $true
            }
        }
    } while ($added)

    return @(
        $allProcesses | Where-Object {
            $processIds.Contains([int]$_.ProcessId)
        }
    )
}

function Write-ResourceSample {
    param(
        [int]$RootProcessId,
        [ref]$PreviousCpuSeconds,
        [ref]$PreviousSampleTime,
        [ref]$PeakRamMb,
        [ref]$PeakCpuPercent
    )

    try {
        $sampleTime = Get-Date
        $tree = @(Get-ProcessTreeSnapshot -RootProcessId $RootProcessId)
        $workingSetBytes = 0.0
        $cpuSeconds = 0.0

        foreach ($item in $tree) {
            $workingSetBytes += [double]$item.WorkingSetSize
            $cpuSeconds += (
                [double]$item.KernelModeTime + [double]$item.UserModeTime
            ) / 10000000.0
        }

        $elapsedSeconds = [Math]::Max(
            0.001,
            ($sampleTime - $PreviousSampleTime.Value).TotalSeconds
        )
        $logicalProcessors = [Math]::Max(1, [Environment]::ProcessorCount)
        $cpuPercent = [Math]::Max(
            0,
            (($cpuSeconds - $PreviousCpuSeconds.Value) / $elapsedSeconds) *
                100 / $logicalProcessors
        )
        $ramMb = $workingSetBytes / 1MB
        $PeakRamMb.Value = [Math]::Max($PeakRamMb.Value, $ramMb)
        $PeakCpuPercent.Value = [Math]::Max($PeakCpuPercent.Value, $cpuPercent)

        $os = Get-CimInstance Win32_OperatingSystem
        $totalRamMb = [double]$os.TotalVisibleMemorySize / 1024
        $freeRamMb = [double]$os.FreePhysicalMemory / 1024
        $systemRamPercent = if ($totalRamMb -gt 0) {
            (($totalRamMb - $freeRamMb) / $totalRamMb) * 100
        }
        else {
            0
        }
        # The daily run resolves its database from config (default
        # config/job_seeker.db) or the project root; probe both so the size
        # metric reports the file actually in use instead of always 0.
        $databaseCandidates = @(
            (Join-Path $projectRoot "config/job_seeker.db"),
            (Join-Path $projectRoot "job_seeker.db")
        )
        $databasePath = $databaseCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
        $databaseMb = if ($databasePath) {
            (Get-Item -LiteralPath $databasePath).Length / 1MB
        }
        else {
            0
        }
        $driveName = (Split-Path -Qualifier $projectRoot).TrimEnd(":")
        $drive = Get-PSDrive -Name $driveName
        $diskFreeGb = [double]$drive.Free / 1GB

        $topProcesses = $tree |
            Group-Object Name |
            ForEach-Object {
                [pscustomobject]@{
                    Name = $_.Name
                    RamMb = (
                        ($_.Group | Measure-Object WorkingSetSize -Sum).Sum / 1MB
                    )
                }
            } |
            Sort-Object RamMb -Descending |
            Select-Object -First 5
        $topText = ($topProcesses | ForEach-Object {
            "$($_.Name):$([Math]::Round($_.RamMb, 1))MB"
        }) -join ","

        $timestamp = $sampleTime.ToString("yyyy-MM-dd HH:mm:ss")
        $line = (
            "[$timestamp] pid=$RootProcessId processes=$($tree.Count) " +
            "cpu=$([Math]::Round($cpuPercent, 1))% " +
            "ram=$([Math]::Round($ramMb, 1))MB " +
            "system_ram=$([Math]::Round($systemRamPercent, 1))% " +
            "free_ram=$([Math]::Round($freeRamMb, 0))MB " +
            "db=$([Math]::Round($databaseMb, 1))MB " +
            "disk_free=$([Math]::Round($diskFreeGb, 1))GB top=$topText"
        )
        Add-Content -LiteralPath $resourceLogPath -Encoding utf8 -Value $line
        $PreviousCpuSeconds.Value = $cpuSeconds
        $PreviousSampleTime.Value = $sampleTime
    }
    catch {
        $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Add-Content -LiteralPath $resourceLogPath -Encoding utf8 -Value (
            "[$timestamp] monitor_error=$($_.Exception.Message)"
        )
    }
}

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
Rotate-LogFile -Path $logPath -PreviousPath $previousLogPath
Rotate-LogFile -Path $resourceLogPath -PreviousPath $previousResourceLogPath

foreach ($temporaryPath in ($stdoutPath, $stderrPath)) {
    if (Test-Path -LiteralPath $temporaryPath) {
        Remove-Item -LiteralPath $temporaryPath -Force
    }
}

$startedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -LiteralPath $logPath -Encoding utf8 -Value "[$startedAt] Daily run started."
Add-Content -LiteralPath $resourceLogPath -Encoding utf8 -Value (
    "[$startedAt] Resource monitoring started. interval=${ResourceSampleSeconds}s"
)

$stdoutLineCount = 0
$stderrLineCount = 0
$previousCpuSeconds = 0.0
$previousSampleTime = Get-Date
$peakRamMb = 0.0
$peakCpuPercent = 0.0
$exitCode = 1

try {
    $process = Start-Process `
        -FilePath $PythonExecutable `
        -ArgumentList @("-u", $mainScript, "--daily-run") `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -PassThru

    try {
        $process.PriorityClass = [System.Diagnostics.ProcessPriorityClass]::BelowNormal
    }
    catch {
        Add-Content -LiteralPath $resourceLogPath -Encoding utf8 -Value (
            "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] " +
            "priority_warning=$($_.Exception.Message)"
        )
    }

    while (-not $process.HasExited) {
        Copy-NewLogLines `
            -SourcePath $stdoutPath `
            -DestinationPath $logPath `
            -CopiedLineCount ([ref]$stdoutLineCount)
        Copy-NewLogLines `
            -SourcePath $stderrPath `
            -DestinationPath $logPath `
            -CopiedLineCount ([ref]$stderrLineCount)
        Write-ResourceSample `
            -RootProcessId $process.Id `
            -PreviousCpuSeconds ([ref]$previousCpuSeconds) `
            -PreviousSampleTime ([ref]$previousSampleTime) `
            -PeakRamMb ([ref]$peakRamMb) `
            -PeakCpuPercent ([ref]$peakCpuPercent)
        Start-Sleep -Seconds $ResourceSampleSeconds
        $process.Refresh()
    }

    $process.WaitForExit()
    Copy-NewLogLines `
        -SourcePath $stdoutPath `
        -DestinationPath $logPath `
        -CopiedLineCount ([ref]$stdoutLineCount)
    Copy-NewLogLines `
        -SourcePath $stderrPath `
        -DestinationPath $logPath `
        -CopiedLineCount ([ref]$stderrLineCount)
    $exitCode = $process.ExitCode
}
catch {
    Add-Content -LiteralPath $logPath -Encoding utf8 -Value "ERROR: $($_.Exception.Message)"
    $exitCode = 1
}

$finishedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -LiteralPath $resourceLogPath -Encoding utf8 -Value (
    "[$finishedAt] Resource monitoring finished. " +
    "peak_cpu=$([Math]::Round($peakCpuPercent, 1))% " +
    "peak_ram=$([Math]::Round($peakRamMb, 1))MB exit_code=$exitCode"
)
Add-Content -LiteralPath $logPath -Encoding utf8 -Value (
    "[$finishedAt] Daily run finished with exit code $exitCode.`n"
)
exit $exitCode
