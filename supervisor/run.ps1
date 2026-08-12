param(
    # 允许计划任务安装器显式传入初始化配置路径。
    [string]$ConfigPath = '',
    # 其余参数原样透传给 Supervisor health 子命令。
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

# 任一命令失败都立即终止，避免计划任务收到虚假的成功退出码。
$ErrorActionPreference = 'Stop'

# 所有路径都以脚本所在目录为基准，避免受计划任务当前目录影响。
$loopRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$entry = Join-Path $PSScriptRoot 'main.py'

# 调用方未覆盖配置时使用项目内的权威初始化配置。
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $loopRoot 'config\initialization.json'
}
$resolvedConfig = [System.IO.Path]::GetFullPath($ConfigPath)

# 在启动 Python 前给出明确的部署错误，避免下层出现难以定位的路径异常。
if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) {
    throw "Supervisor 入口不存在: $entry"
}
if (-not (Test-Path -LiteralPath $resolvedConfig -PathType Leaf)) {
    throw "初始化配置不存在: $resolvedConfig"
}

# 固定使用 Windows Python Launcher 的 Python 3，并禁止生成字节码缓存。
$pythonLauncher = (Get-Command py.exe -ErrorAction Stop).Source
$arguments = @('-3', '-B', $entry, 'health', '--config', $resolvedConfig) + $SupervisorArguments

# 健康检查在项目根目录执行，确保相对路径和模块导入保持一致。
Push-Location -LiteralPath $loopRoot
try {
    & $pythonLauncher @arguments
    # 将 Python 的健康检查结果原样传给 Windows 计划任务历史。
    exit $LASTEXITCODE
}
finally {
    # 即使 Python 启动失败，也恢复调用者原来的工作目录。
    Pop-Location
}
