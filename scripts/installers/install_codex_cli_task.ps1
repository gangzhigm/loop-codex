param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot '..\..\config\initialization.json'),
    [switch]$DryRun
)

<#
中文排查：按 initialization.json 注册 Codex CLI Dispatcher 的 Windows 计划任务。
先使用 -DryRun 核对任务名、周期、工作目录和完整命令；只有明确部署时才允许真正注册。
安装失败依次检查配置、py.exe、Dispatcher 路径、当前用户身份和任务计划程序权限。
#>

$ErrorActionPreference = 'Stop'
$loopRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$resolvedConfig = [System.IO.Path]::GetFullPath($ConfigPath)
$config = Get-Content -Raw -Encoding UTF8 -LiteralPath $resolvedConfig | ConvertFrom-Json
$dispatcher = $config.codex_cli.dispatcher

if ($null -eq $dispatcher -or [int]$dispatcher.interval_minutes -lt 1 -or [string]::IsNullOrWhiteSpace([string]$dispatcher.task_name)) {
    throw 'codex_cli.dispatcher settings are invalid'
}
if ($dispatcher.working_directory -ne $loopRoot -or $dispatcher.run_as_current_user -ne $true -or $dispatcher.hidden -ne $true) {
    throw 'dispatcher must use the Loop root, current user, and hidden execution'
}

$pythonLauncher = (Get-Command py.exe -ErrorAction Stop).Source
$dispatcherScript = Join-Path $loopRoot 'scripts\roles\dispatcher\codex_cli_dispatcher.py'
$arguments = "-3 -B `"$dispatcherScript`" --config `"$resolvedConfig`""
$preview = [ordered]@{
    outcome = if ($DryRun) { 'DRY_RUN' } else { 'INSTALLED' }
    task_name = [string]$dispatcher.task_name
    interval_minutes = [int]$dispatcher.interval_minutes
    working_directory = $loopRoot
    run_as_current_user = $true
    hidden = $true
    command = "$pythonLauncher $arguments"
}
if ($DryRun) {
    $preview | ConvertTo-Json
    return
}

$action = New-ScheduledTaskAction -Execute $pythonLauncher -Argument $arguments -WorkingDirectory $loopRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes ([int]$dispatcher.interval_minutes))
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -Hidden -ExecutionTimeLimit (New-TimeSpan -Seconds ([int]$dispatcher.timeout_seconds))
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName ([string]$dispatcher.task_name) -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Local Agent Loop single-dispatch Codex CLI launcher.' -Force | Out-Null
$preview | ConvertTo-Json
