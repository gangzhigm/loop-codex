param(
    # 可覆盖安装时读取的初始化配置文件。
    [string]$ConfigPath = '',
    # 注册完成后是否立即触发一次健康检查。
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

# 将任何安装错误直接暴露给调用者，禁止静默注册不完整任务。
$ErrorActionPreference = 'Stop'

# 从脚本目录解析项目根目录，保证计划任务可从任意工作目录安装。
$loopRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $loopRoot 'config\initialization.json'
}

$resolvedConfig = [System.IO.Path]::GetFullPath($ConfigPath)
$launcher = Join-Path $PSScriptRoot 'run.ps1'

# 配置和启动器缺失时停止安装，避免注册一个必然失败的计划任务。
if (-not (Test-Path -LiteralPath $resolvedConfig -PathType Leaf)) {
    throw "初始化配置不存在: $resolvedConfig"
}
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "Supervisor 启动器不存在: $launcher"
}

# 初始化配置是任务名称和执行周期的唯一来源。
$config = Get-Content -Raw -Encoding UTF8 -LiteralPath $resolvedConfig | ConvertFrom-Json
if ($config.health.scheduler -ne 'windows_task_scheduler') {
    throw 'health.scheduler 必须为 windows_task_scheduler'
}

$taskName = [string]$config.health.task_name
$interval = [int]$config.health.interval_minutes
if ([string]::IsNullOrWhiteSpace($taskName) -or $interval -lt 1) {
    throw 'health.task_name 或 health.interval_minutes 无效'
}

# 计划任务调用隐藏的 Windows PowerShell，并把配置路径安全地加引号。
$powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`" -ConfigPath `"$resolvedConfig`""

# Action 固定运行健康启动器，工作目录固定为项目根目录。
$action = New-ScheduledTaskAction `
    -Execute $powershell `
    -Argument $arguments `
    -WorkingDirectory $loopRoot

# 首次运行安排在一分钟后，之后按配置周期重复。
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $interval)

# 允许电池供电和错过后补跑，同时拒绝并发健康检查实例。
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

# 同名任务存在时原地更新，保持部署命令幂等。
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

# 注册后重新读取任务事实，以 JSON 返回实际状态和调度时间。
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
