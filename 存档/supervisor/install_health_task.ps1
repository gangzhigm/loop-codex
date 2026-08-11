param(
    [string]$ConfigPath = '',
    [switch]$StartNow
)

<#
注册或更新 Supervisor 健康检查的 Windows 计划任务。

任务按 initialization.json 中 health.interval_minutes 的周期启动本目录的 run.ps1；该
启动器再运行一次 health 检查。安装脚本本身不领取业务任务、不调用 AI，也不改变 SQLite
的任务数据。重复运行会更新同名计划任务。

使用 -StartNow 只会在注册完成后额外启动一次任务。日常安装无需该参数。排查失败时先检查
当前账户是否可以注册计划任务、py.exe 是否可用，再检查 Supervisor 启动器与配置文件路径。
#>

$ErrorActionPreference = 'Stop'

$loopRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $loopRoot 'config\initialization.json'
}

$resolvedConfig = [System.IO.Path]::GetFullPath($ConfigPath)
$launcher = Join-Path $PSScriptRoot 'run.ps1'

if (-not (Test-Path -LiteralPath $resolvedConfig -PathType Leaf)) {
    throw "初始化配置不存在: $resolvedConfig"
}
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "Supervisor 启动器不存在: $launcher"
}

$config = Get-Content -Raw -Encoding UTF8 -LiteralPath $resolvedConfig | ConvertFrom-Json
if ($config.health.scheduler -ne 'windows_task_scheduler') {
    throw 'health.scheduler 必须为 windows_task_scheduler'
}

$taskName = [string]$config.health.task_name
$interval = [int]$config.health.interval_minutes
if ([string]::IsNullOrWhiteSpace($taskName) -or $interval -lt 1) {
    throw 'health.task_name 或 health.interval_minutes 无效'
}

$powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`" -ConfigPath `"$resolvedConfig`""

$action = New-ScheduledTaskAction `
    -Execute $powershell `
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
    -Description 'Local Agent Loop Supervisor dashboard health check.' `
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
