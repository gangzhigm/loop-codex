# 说明下一条脚本语句的作用。
param(
    # 说明下一条脚本语句的作用。
    [string]$ConfigPath = '',
    # 说明下一条脚本语句的作用。
    [switch]$StartNow
# 说明下一条脚本语句的作用。
)

<#
注册或更新 Supervisor 健康检查的 Windows 计划任务。

任务按 initialization.json 中 health.interval_minutes 的周期启动本目录的 run.ps1；该
启动器再运行一次 health 检查。安装脚本本身不领取业务任务、不调用 AI，也不改变 SQLite
的任务数据。重复运行会更新同名计划任务。

使用 -StartNow 只会在注册完成后额外启动一次任务。日常安装无需该参数。排查失败时先检查
当前账户是否可以注册计划任务、py.exe 是否可用，再检查 Supervisor 启动器与配置文件路径。
#>

# 说明下一条脚本语句的作用。
$ErrorActionPreference = 'Stop'

# 说明下一条脚本语句的作用。
$loopRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
# 说明下一条脚本语句的作用。
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    # 说明下一条脚本语句的作用。
    $ConfigPath = Join-Path $loopRoot 'config\initialization.json'
# 说明下一条脚本语句的作用。
}

# 说明下一条脚本语句的作用。
$resolvedConfig = [System.IO.Path]::GetFullPath($ConfigPath)
# 说明下一条脚本语句的作用。
$launcher = Join-Path $PSScriptRoot 'run.ps1'

# 说明下一条脚本语句的作用。
if (-not (Test-Path -LiteralPath $resolvedConfig -PathType Leaf)) {
    # 说明下一条脚本语句的作用。
    throw "初始化配置不存在: $resolvedConfig"
# 说明下一条脚本语句的作用。
}
# 说明下一条脚本语句的作用。
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    # 说明下一条脚本语句的作用。
    throw "Supervisor 启动器不存在: $launcher"
# 说明下一条脚本语句的作用。
}

# 说明下一条脚本语句的作用。
$config = Get-Content -Raw -Encoding UTF8 -LiteralPath $resolvedConfig | ConvertFrom-Json
# 说明下一条脚本语句的作用。
if ($config.health.scheduler -ne 'windows_task_scheduler') {
    # 说明下一条脚本语句的作用。
    throw 'health.scheduler 必须为 windows_task_scheduler'
# 说明下一条脚本语句的作用。
}

# 说明下一条脚本语句的作用。
$taskName = [string]$config.health.task_name
# 说明下一条脚本语句的作用。
$interval = [int]$config.health.interval_minutes
# 说明下一条脚本语句的作用。
if ([string]::IsNullOrWhiteSpace($taskName) -or $interval -lt 1) {
    # 说明下一条脚本语句的作用。
    throw 'health.task_name 或 health.interval_minutes 无效'
# 说明下一条脚本语句的作用。
}

# 说明下一条脚本语句的作用。
$powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
# 说明下一条脚本语句的作用。
$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`" -ConfigPath `"$resolvedConfig`""

# 说明下一条脚本语句的作用。
$action = New-ScheduledTaskAction `
    # 说明下一条脚本语句的作用。
    -Execute $powershell `
    # 说明下一条脚本语句的作用。
    -Argument $arguments `
    # 说明下一条脚本语句的作用。
    -WorkingDirectory $loopRoot
# 说明下一条脚本语句的作用。
$trigger = New-ScheduledTaskTrigger `
    # 说明下一条脚本语句的作用。
    -Once `
    # 说明下一条脚本语句的作用。
    -At (Get-Date).AddMinutes(1) `
    # 说明下一条脚本语句的作用。
    -RepetitionInterval (New-TimeSpan -Minutes $interval)
# 说明下一条脚本语句的作用。
$settings = New-ScheduledTaskSettingsSet `
    # 说明下一条脚本语句的作用。
    -AllowStartIfOnBatteries `
    # 说明下一条脚本语句的作用。
    -DontStopIfGoingOnBatteries `
    # 说明下一条脚本语句的作用。
    -StartWhenAvailable `
    # 说明下一条脚本语句的作用。
    -MultipleInstances IgnoreNew `
    # 说明下一条脚本语句的作用。
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

# 说明下一条脚本语句的作用。
Register-ScheduledTask `
    # 说明下一条脚本语句的作用。
    -TaskName $taskName `
    # 说明下一条脚本语句的作用。
    -Action $action `
    # 说明下一条脚本语句的作用。
    -Trigger $trigger `
    # 说明下一条脚本语句的作用。
    -Settings $settings `
    # 说明下一条脚本语句的作用。
    -Description 'Local Agent Loop Supervisor dashboard health check.' `
    # 说明下一条脚本语句的作用。
    -Force | Out-Null

# 说明下一条脚本语句的作用。
if ($StartNow) {
    # 说明下一条脚本语句的作用。
    Start-ScheduledTask -TaskName $taskName
# 说明下一条脚本语句的作用。
}

# 说明下一条脚本语句的作用。
$task = Get-ScheduledTask -TaskName $taskName
# 说明下一条脚本语句的作用。
$info = Get-ScheduledTaskInfo -TaskName $taskName
# 说明下一条脚本语句的作用。
[ordered]@{
    # 说明下一条脚本语句的作用。
    outcome = 'INSTALLED'
    # 说明下一条脚本语句的作用。
    task_name = $task.TaskName
    # 说明下一条脚本语句的作用。
    state = [string]$task.State
    # 说明下一条脚本语句的作用。
    interval_minutes = $interval
    # 说明下一条脚本语句的作用。
    last_run_time = $info.LastRunTime.ToString('o')
    # 说明下一条脚本语句的作用。
    next_run_time = $info.NextRunTime.ToString('o')
# 说明下一条脚本语句的作用。
} | ConvertTo-Json
