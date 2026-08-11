# 说明下一条脚本语句的作用。
param(
    # 说明下一条脚本语句的作用。
    [string]$ConfigPath = '',
    # 说明下一条脚本语句的作用。
    [Parameter(ValueFromRemainingArguments = $true)]
    # 说明下一条脚本语句的作用。
    [string[]]$SupervisorArguments
# 说明下一条脚本语句的作用。
)

<#
Windows 计划任务调用的 Supervisor 启动器。

脚本固定执行一次 Supervisor 健康检查与必要恢复，适合设置为周期任务。检查发现主进程
不健康时，Python 健康检查会在后台启动 ``main.py serve``，随后本脚本退出。此脚本不
注册、启动或修改 Windows 计划任务，也不直接访问 SQLite。

排查计划任务失败时，依次确认 py.exe 可用、main.py 存在、初始化配置路径正确，再查看
Supervisor 输出的 JSON 或运行时日志。退出码原样返回给 Windows 计划任务。
#>

# 说明下一条脚本语句的作用。
$ErrorActionPreference = 'Stop'

# 说明下一条脚本语句的作用。
$loopRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
# 说明下一条脚本语句的作用。
$entry = Join-Path $PSScriptRoot 'main.py'
# 说明下一条脚本语句的作用。
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    # 说明下一条脚本语句的作用。
    $ConfigPath = Join-Path $loopRoot 'config\initialization.json'
# 说明下一条脚本语句的作用。
}
# 说明下一条脚本语句的作用。
$resolvedConfig = [System.IO.Path]::GetFullPath($ConfigPath)

# 说明下一条脚本语句的作用。
if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) {
    # 说明下一条脚本语句的作用。
    throw "Supervisor 入口不存在: $entry"
# 说明下一条脚本语句的作用。
}
# 说明下一条脚本语句的作用。
if (-not (Test-Path -LiteralPath $resolvedConfig -PathType Leaf)) {
    # 说明下一条脚本语句的作用。
    throw "初始化配置不存在: $resolvedConfig"
# 说明下一条脚本语句的作用。
}

# 说明下一条脚本语句的作用。
$pythonLauncher = (Get-Command py.exe -ErrorAction Stop).Source
# 说明下一条脚本语句的作用。
$arguments = @('-3', '-B', $entry, 'health', '--config', $resolvedConfig) + $SupervisorArguments

# 说明下一条脚本语句的作用。
Push-Location -LiteralPath $loopRoot
# 说明下一条脚本语句的作用。
try {
    # 说明下一条脚本语句的作用。
    & $pythonLauncher @arguments
    # 说明下一条脚本语句的作用。
    exit $LASTEXITCODE
# 说明下一条脚本语句的作用。
}
# 说明下一条脚本语句的作用。
finally {
    # 说明下一条脚本语句的作用。
    Pop-Location
# 说明下一条脚本语句的作用。
}
