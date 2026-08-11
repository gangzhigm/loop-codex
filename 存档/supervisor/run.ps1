param(
    [string]$ConfigPath = '',
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$SupervisorArguments
)

<#
Windows 计划任务调用的 Supervisor 启动器。

脚本固定执行一次 Supervisor 健康检查与必要恢复，适合设置为周期任务。检查发现主进程
不健康时，Python 健康检查会在后台启动 ``main.py serve``，随后本脚本退出。此脚本不
注册、启动或修改 Windows 计划任务，也不直接访问 SQLite。

排查计划任务失败时，依次确认 py.exe 可用、main.py 存在、初始化配置路径正确，再查看
Supervisor 输出的 JSON 或运行时日志。退出码原样返回给 Windows 计划任务。
#>

$ErrorActionPreference = 'Stop'

$loopRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$entry = Join-Path $PSScriptRoot 'main.py'
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $loopRoot 'config\initialization.json'
}
$resolvedConfig = [System.IO.Path]::GetFullPath($ConfigPath)

if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) {
    throw "Supervisor 入口不存在: $entry"
}
if (-not (Test-Path -LiteralPath $resolvedConfig -PathType Leaf)) {
    throw "初始化配置不存在: $resolvedConfig"
}

$pythonLauncher = (Get-Command py.exe -ErrorAction Stop).Source
$arguments = @('-3', '-B', $entry, 'health', '--config', $resolvedConfig) + $SupervisorArguments

Push-Location -LiteralPath $loopRoot
try {
    & $pythonLauncher @arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
