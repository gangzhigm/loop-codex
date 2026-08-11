param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot '..\..\config\initialization.json'),
    [switch]$StartNow
)

<#
中文排查：注册或更新 Dashboard 健康检查计划任务，并可通过 -StartNow 立即运行一次。
异常先核对配置中的任务名和 30 分钟周期，再检查 py.exe、Supervisor 路径及账户权限。
该任务只运行 health_run.py，不调用模型、不领取业务任务。
#>

$ErrorActionPreference = 'Stop'
$loopRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
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
$healthScript = Join-Path $loopRoot 'scripts\roles\supervisor\health_run.py'
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
