[CmdletBinding()]
param(
    [string]$ConfigPath
)

<#
部署校验：只读核对当前真实初始化配置、角色提示词、Planner 边界和内部执行环境。
失败时从输出的第一条“初始化检查失败”开始处理，后续错误可能只是同一路径或配置根因。
本脚本不创建数据库、不注册计划任务，也不启动 Scheduler 或 Runner。
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

Assert-Condition ($config.config_version -eq '5.0.0') 'config_version 为 5.0.0'
Assert-Condition ($config.database.schema_version -eq '3.7.0') 'Schema 契约为 3.7.0'
Assert-Condition ($config.prompts.planner -eq 'planner/planner.md') 'Planner 提示词路径唯一且已登记'

$promptPaths = @(
    $config.prompts.operator,
    $config.prompts.planner,
    $config.prompts.worker
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

$plannerScheduler = $config.planner.scheduler
Assert-Condition ($plannerScheduler.scheduled -is [bool]) 'Planner Scheduler 开关为布尔值'
Assert-Condition ($plannerScheduler.interval_minutes -ge 1) 'Planner Scheduler 周期有效'
Assert-Condition ($plannerScheduler.heartbeat_interval_seconds -ge 1) 'Planner Scheduler heartbeat 周期有效'
Assert-Condition ($config.planner.default_runtime_environment -eq 'self_hosted_agent') 'Planner 默认目标为内部 Agent 环境'
Assert-Condition ($config.planner.provider_id -eq 'deepseek') 'Planner 显式登记内部 Provider'
Assert-Condition ($null -eq $config.automations) '初始化配置不包含外部客户端自动化定义'
$runtimeNames = @($config.runtime_environments.PSObject.Properties.Name | Sort-Object)
Assert-Condition (($runtimeNames -join '|') -eq 'self_hosted_agent') '只登记内部 Agent 运行环境'
$profileNames = @($config.execution_profiles.PSObject.Properties.Name | Sort-Object)
Assert-Condition (($profileNames -join '|') -eq 'self_hosted_agent') '只登记内部 Agent execution profile'
Assert-Condition ($config.dispatcher.runtime_environment -eq 'self_hosted_agent') 'Dispatcher 只分发内部 Agent 任务'
Assert-Condition ($config.dispatcher.provider_id -eq 'deepseek') 'Dispatcher 显式登记内部 Provider'

$operatorPrompt = Read-Utf8Strict -Path (Join-Path $root $config.prompts.operator)
$plannerPrompt = Read-Utf8Strict -Path (Join-Path $root $config.prompts.planner)
$workerPrompt = Read-Utf8Strict -Path (Join-Path $root $config.prompts.worker)
$loopctlSource = Read-Utf8Strict -Path (Join-Path $root 'control\loopctl.py')
$plannerControlSource = Read-Utf8Strict -Path (Join-Path $root 'planner\control.py')
$controlIoSource = Read-Utf8Strict -Path (Join-Path $root 'control\loop_agent\control\io.py')
Assert-Condition ($plannerPrompt -match 'preflight-claim') 'Planner 提示词包含单次 claim 协议'
Assert-Condition ($plannerPrompt -match 'sandbox=read-only') 'Planner 提示词核对只读入口'
Assert-Condition ($plannerPrompt -match 'preflight-needs-review') 'Planner 提示词包含人工复核分支'
Assert-Condition ($plannerPrompt -match 'Unicode replacement character') 'Planner 提示词要求 UTF-8 写回完整性检查'
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
Assert-Condition ($config.self_hosted_agent.provider_factories.deepseek -eq 'loop_agent.providers.deepseek:create_provider') '内部 Provider 工厂路径已登记'

[ordered]@{
    outcome = 'VALID'
    config = $resolvedConfig
    checks = $script:Checks.Count
    runtime_environment = 'self_hosted_agent'
    operator_actions = @(
        '复核 Planner Scheduler 的启停、轮询周期和 heartbeat 边界。',
        '逐条复核内部运行环境 L1-L5 的模型、reasoning 和 attempt 参数。',
        '确认 Planner 只暴露受控 loopctl preflight stdin 写回，不授予直接文件或 SQLite 写权限。',
        '分别验证 Planner 和内部 Runner 的 NO_TASK 路径。'
    )
} | ConvertTo-Json -Depth 5
