param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot '..\config\initialization.json'),
    [switch]$StartNow
)

$ErrorActionPreference = 'Stop'
$loopRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$resolvedConfig = [System.IO.Path]::GetFullPath($ConfigPath)
$config = Get-Content -Raw -Encoding UTF8 -LiteralPath $resolvedConfig | ConvertFrom-Json

if ($config.health.scheduler -ne 'windows_task_scheduler') {
    throw 'health.scheduler must be windows_task_scheduler'
}

$taskName = [string]$config.health.task_name
$interval = [int]$config.health.interval_minutes
if ([string]::IsNullOrWhiteSpace($taskName) -or $interval -lt 1) {
    throw 'health task_name or interval_minutes is invalid'
}

$pythonLauncher = (Get-Command py.exe -ErrorAction Stop).Source
$healthScript = Join-Path $loopRoot 'scripts\health_run.py'
$database = Join-Path $loopRoot ([string]$config.database.path)
$arguments = "-3 -B `"$healthScript`" --db `"$database`" --config `"$resolvedConfig`""

$action = New-ScheduledTaskAction `
    -Execute $pythonLauncher `
    -Argument $arguments `
    -WorkingDirectory $loopRoot
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $interval)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description 'Local Agent Loop dashboard and SQLite health check.' `
    -Force | Out-Null

if ($StartNow) {
    Start-ScheduledTask -TaskName $taskName
}

$task = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
[ordered]@{
    outcome = 'INSTALLED'
    task_name = $task.TaskName
    state = [string]$task.State
    interval_minutes = $interval
    last_run_time = $info.LastRunTime.ToString('o')
    next_run_time = $info.NextRunTime.ToString('o')
} | ConvertTo-Json
