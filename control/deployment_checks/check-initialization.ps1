[CmdletBinding()]
param(
    [string]$ConfigPath,
    [switch]$SkipCodexCliCheck
)

<#
部署校验：只读核对当前真实初始化配置、角色提示词、Planner 边界和五档 Worker 定义。
失败时从输出的第一条“初始化检查失败”开始处理，后续错误可能只是同一路径或配置根因。
本脚本不创建数据库、不注册计划任务，也不修改 Codex 自动化。
#>

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$script:Checks = [System.Collections.Generic.List[string]]::new()

if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $controlRoot = Split-Path -Parent $PSScriptRoot
    $ConfigPath = Join-Path (Split-Path -Parent $controlRoot) 'config\initialization.json'
}

function Assert-Condition {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Message
    )
    if (-not $Condition) {
        throw "初始化检查失败：$Message"
    }
    $script:Checks.Add($Message)
}

function Read-Utf8Strict {
    param([Parameter(Mandatory = $true)][string]$Path)
    $encoding = [System.Text.UTF8Encoding]::new($false, $true)
    return [System.IO.File]::ReadAllText((Resolve-Path -LiteralPath $Path), $encoding)
}

$resolvedConfig = (Resolve-Path -LiteralPath $ConfigPath).Path
$root = Split-Path -Parent (Split-Path -Parent $resolvedConfig)
$configText = Read-Utf8Strict -Path $resolvedConfig
$config = $configText | ConvertFrom-Json

Assert-Condition ($config.config_version -eq '4.3.0') 'config_version 为 4.3.0'
Assert-Condition ($config.database.schema_version -eq '3.7.0') 'Schema 契约为 3.7.0'
Assert-Condition ($config.prompts.planner -eq 'planner/planner.md') 'Planner 提示词路径唯一且已登记'

$promptPaths = @(
    $config.prompts.operator,
    $config.prompts.planner,
    $config.prompts.worker,
    $config.prompts.cli_worker
)
foreach ($relativePath in $promptPaths) {
    $fullPath = Join-Path $root $relativePath
    Assert-Condition (Test-Path -LiteralPath $fullPath -PathType Leaf) "提示词存在：$relativePath"
    $null = Read-Utf8Strict -Path $fullPath
}

$boundary = $config.planner.client_boundary
Assert-Condition ($boundary.sandbox -eq 'read-only') 'Planner sandbox 固定为 read-only'
Assert-Condition ($boundary.approval_policy -eq 'never') 'Planner 禁止运行时权限提升'
Assert-Condition ($boundary.network_access -eq $false) 'Planner 禁止网络访问'
Assert-Condition ($boundary.default_tool_action -eq 'deny') 'Planner 工具策略默认拒绝'
Assert-Condition ($boundary.source_access -eq 'read-only') 'Planner 业务源文件只读'
Assert-Condition ($boundary.writeback.transport -eq 'host_controlled_loopctl_stdin') 'Planner 使用宿主受控 stdin 写回'
Assert-Condition ($boundary.writeback.payload_encoding -eq 'utf-8') 'Planner 写回 payload 固定为 UTF-8'
Assert-Condition ($boundary.writeback.integrity_policy -eq 'reject_suspicious_question_mark_corruption') 'Planner 写回拒绝明显问号损坏'
Assert-Condition ($boundary.writeback.direct_sql -eq $false) 'Planner 禁止直接 SQL'
Assert-Condition ($boundary.writeback.report_files -eq $false) 'Planner 禁止 report 文件'
$expectedWriteback = @(
    'preflight-claim',
    'preflight-heartbeat',
    'preflight-ready',
    'preflight-needs-review',
    'preflight-fail'
)
Assert-Condition (
    (($boundary.writeback.allowed_commands -join '|') -eq ($expectedWriteback -join '|'))
) 'Planner 写回命令为精确允许列表'

$plannerAutomation = $config.automations.planner
Assert-Condition ($plannerAutomation.automation_id -eq 'loop-agent-planner') 'Planner 自动化 ID 固定'
Assert-Condition ($plannerAutomation.scheduled -is [bool]) 'Planner 自动调度开关为布尔值'
Assert-Condition ($plannerAutomation.interval_minutes -eq 5) 'Planner 周期为 5 分钟'
Assert-Condition ($plannerAutomation.model -eq 'gpt-5.6-terra') 'Planner 模型为 Terra'
Assert-Condition ($plannerAutomation.reasoning_effort -eq 'high') 'Planner reasoning 为 high'
Assert-Condition ($plannerAutomation.runtime_environment -eq 'codex_automation') 'Planner 环境为 codex_automation'
Assert-Condition ($plannerAutomation.execution_kind -eq 'PLANNER') 'Planner execution kind 已隔离'
Assert-Condition ($plannerAutomation.sandbox -eq 'read-only') 'Planner 自动化声明只读沙箱'
Assert-Condition ($plannerAutomation.approval_policy -eq 'never') 'Planner 自动化禁止审批升级'
Assert-Condition ($plannerAutomation.entry_prompt -match 'planner\\planner\.md') 'Planner 入口只引用权威提示词'
Assert-Condition ($plannerAutomation.entry_prompt -match 'runtime_environment=codex_automation') 'Planner 入口显式声明运行环境'
Assert-Condition ($plannerAutomation.entry_prompt -match 'execution_kind=PLANNER') 'Planner 入口显式声明角色'
Assert-Condition ($plannerAutomation.entry_prompt -match 'sandbox=read-only') 'Planner 入口显式声明只读边界'

$expectedWorkers = [ordered]@{
    L1 = @('gpt-5.6-luna', 'medium', 0)
    L2 = @('gpt-5.6-terra', 'medium', 2)
    L3 = @('gpt-5.6-terra', 'high', 4)
    L4 = @('gpt-5.6-sol', 'high', 6)
    L5 = @('gpt-5.6-sol', 'xhigh', 8)
}
Assert-Condition ($config.automations.worker_interval_minutes -eq 20) 'Worker 周期为 20 分钟'
foreach ($level in $expectedWorkers.Keys) {
    $worker = $config.automations.capabilities.$level
    $expected = $expectedWorkers[$level]
    Assert-Condition ($worker.scheduled -eq $true) "$level Worker 已登记为定时任务"
    Assert-Condition ($worker.execution_policy -eq 'automatic') "$level Worker 策略为 automatic"
    Assert-Condition ($worker.model -eq $expected[0]) "$level Worker 模型固定"
    Assert-Condition ($worker.reasoning_effort -eq $expected[1]) "$level Worker reasoning 固定"
    Assert-Condition ($worker.offset_minutes -eq $expected[2]) "$level Worker 错峰固定"
}
Assert-Condition ($config.automations.entry_prompt_template -match 'worker\\worker\.md') 'Worker 入口只引用权威提示词'
Assert-Condition ($config.automations.entry_prompt_template -match 'capability_level=\{capability_level\}') 'Worker 入口显式传递能力等级'
Assert-Condition ($config.automations.entry_prompt_template -match 'execution_policy=automatic') 'Worker 入口显式传递执行策略'

$operatorPrompt = Read-Utf8Strict -Path (Join-Path $root $config.prompts.operator)
$plannerPrompt = Read-Utf8Strict -Path (Join-Path $root $config.prompts.planner)
$workerPrompt = Read-Utf8Strict -Path (Join-Path $root $config.prompts.worker)
$cliPrompt = Read-Utf8Strict -Path (Join-Path $root $config.prompts.cli_worker)
$loopctlSource = Read-Utf8Strict -Path (Join-Path $root 'control\loopctl.py')
$plannerControlSource = Read-Utf8Strict -Path (Join-Path $root 'planner\control.py')
$controlIoSource = Read-Utf8Strict -Path (Join-Path $root 'control\loop_agent\control\io.py')
Assert-Condition ($plannerPrompt -match 'preflight-claim') 'Planner 提示词包含单次 claim 协议'
Assert-Condition ($plannerPrompt -match '--sandbox read-only') 'Planner 提示词核对只读入口'
Assert-Condition ($plannerPrompt -match 'preflight-needs-review') 'Planner 提示词包含人工复核分支'
Assert-Condition ($plannerPrompt -match '控制面会拒绝明显损坏的 payload') 'Planner 提示词要求 UTF-8 写回完整性检查'
Assert-Condition ($plannerPrompt -match 'APPROVED_PLANNER_ESCALATION') 'Planner 提示词要求 L5/manual 明确批准'
Assert-Condition ($plannerPrompt -match '不得实现任务') 'Planner 提示词禁止实现业务任务'
Assert-Condition ($operatorPrompt -match 'APPROVED_PLANNER_ESCALATION') 'Operator 提示词记录 L5/manual 批准'
Assert-Condition (
    ($loopctlSource -match 'from planner\.control import') -and
    ($plannerControlSource -match 'planner_escalation_is_approved')
) '控制面执行 L5/manual 批准门禁'
Assert-Condition (
    ($plannerControlSource -match 'read_preflight_report') -and
    ($controlIoSource -match 'source != "-"')
) '控制面强制 Planner stdin 结果'
Assert-Condition (
    ($plannerControlSource -match 'validate_preflight_text_integrity') -and
    ($controlIoSource -match 'SUSPICIOUS_QUESTION_MARK_RUN')
) '控制面拒绝损坏的 Planner 文本'
Assert-Condition ($workerPrompt -match 'scope_lock_credential') 'Worker 编辑前核对锁凭证'
Assert-Condition ($workerPrompt -match 'extend-scope') 'Worker 新范围写入前原子扩锁'
Assert-Condition ($workerPrompt -match '唯一允许自动恢复的已跟踪范围外文件') 'Worker 仅允许自动恢复严格证明归属的已跟踪字节码缓存'
Assert-Condition ($workerPrompt -match '不得删除文件、修改索引、使用通配符或递归操作') 'Worker 字节码恢复不得扩大为删除或批量回退权限'
Assert-Condition ($cliPrompt -match '唯一允许自动恢复的已跟踪范围外文件') 'CLI Worker 使用相同的已跟踪字节码缓存恢复边界'
Assert-Condition ($cliPrompt -match 'PENDING/READY') 'CLI Worker 只执行 READY 任务'
Assert-Condition ($cliPrompt -match 'extend-scope') 'CLI Worker 遵循宿主扩锁契约'

$codexVersion = $null
if (-not $SkipCodexCliCheck) {
    $codexCommand = Get-Command codex -ErrorAction Stop
    $codexVersion = (& $codexCommand.Source --version 2>&1 | Out-String).Trim()
    Assert-Condition ($LASTEXITCODE -eq 0) '本机 Codex CLI 可执行'
    $codexHelp = (& $codexCommand.Source --help 2>&1 | Out-String)
    Assert-Condition ($LASTEXITCODE -eq 0) '本机 Codex CLI help 可读取'
    Assert-Condition ($codexHelp -match 'read-only') '本机 Codex CLI 明确支持 read-only sandbox'
    Assert-Condition ($codexHelp -match 'dangerously-bypass-approvals-and-sandbox') '初始化检查识别危险 bypass 参数'
}

[ordered]@{
    outcome = 'VALID'
    config = $resolvedConfig
    checks = $script:Checks.Count
    codex_cli = $codexVersion
    operator_actions = @(
        '创建并复核独立 Planner 自动化的 5 分钟周期、Terra/high 和 read-only 边界。',
        '逐条更新并复核 L1-L5 Worker 的模型、reasoning、20 分钟周期、错峰和入口参数。',
        '确认 Planner 只暴露受控 loopctl preflight stdin 写回，不授予直接文件或 SQLite 写权限。',
        '完成真实自动化切换后分别验证 Planner NO_TASK 与五条 Worker NO_TASK。'
    )
} | ConvertTo-Json -Depth 5
