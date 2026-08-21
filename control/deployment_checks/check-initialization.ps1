[CmdletBinding()]
param(
    [string]$ConfigPath
)

<#
部署校验：只读核对当前真实初始化配置、Scheduler/Planner 交付边界和内部执行环境。
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

Assert-Condition ($config.config_version -eq '5.6.0') 'config_version 为 5.6.0'
Assert-Condition ($config.database.schema_version -eq '3.10.0') 'Schema 契约为 3.10.0'
Assert-Condition ($config.prompts.planner -eq 'scheduler/planner.md') 'Planner 提示词路径唯一且已登记'

$promptPaths = @(
    $config.prompts.operator,
    $config.prompts.planner,
    $config.prompts.worker,
    $config.codex_cli.prompt
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
    'preflight-split',
    'preflight-needs-review',
    'preflight-fail'
)
Assert-Condition (
    (($boundary.writeback.allowed_commands -join '|') -eq ($expectedWriteback -join '|'))
) 'Planner 写回命令为精确允许列表'

$scheduler = $config.scheduler
$preflightScheduler = $scheduler.preflight
Assert-Condition ($scheduler.heartbeat_interval_seconds -ge 1) 'Scheduler heartbeat 周期有效'
Assert-Condition ($preflightScheduler.scheduled -is [bool]) 'Scheduler 预检排队开关为布尔值'
Assert-Condition ($preflightScheduler.interval_minutes -ge 1) 'Scheduler 预检排队周期有效'
Assert-Condition ($config.planner.default_runtime_environment -eq 'self_hosted_agent') 'Planner 默认目标为内部 Agent 环境'
Assert-Condition ($config.planner.provider_id -eq 'deepseek') 'Planner 显式登记内部 Provider'
Assert-Condition ($config.planner.worker_runtime_environment -eq 'codex_cli') 'Planner Worker 使用 Codex CLI'
Assert-Condition ($null -eq $config.planner.worker_provider_id) 'Planner Codex Worker 不伪造 Provider'
Assert-Condition ($config.planner.capability_level -eq 'L3') 'Planner Worker 固定使用 L3'
Assert-Condition ($null -eq $config.automations) '初始化配置不包含外部客户端自动化定义'
$runtimeNames = @($config.runtime_environments.PSObject.Properties.Name | Sort-Object)
Assert-Condition (($runtimeNames -join '|') -eq 'codex_cli|self_hosted_agent') '登记 Runner 可管理的内部执行环境'
$profileNames = @($config.execution_profiles.PSObject.Properties.Name | Sort-Object)
Assert-Condition (($profileNames -join '|') -eq 'codex_cli|self_hosted_agent') '登记 Worker execution profiles'
$executionScheduler = $scheduler.execution
Assert-Condition ($executionScheduler.scheduled -is [bool]) 'Scheduler 执行分发开关为布尔值'
Assert-Condition ($executionScheduler.interval_minutes -ge 1) 'Scheduler 执行分发周期有效'
Assert-Condition ($executionScheduler.max_tasks_per_cycle -ge 1) 'Dispatcher 单轮排队上限有效'
Assert-Condition ($config.runner.heartbeat_interval_seconds -ge 1) 'Runner heartbeat 周期有效'
Assert-Condition ($config.runner.queue_poll_interval_seconds -ge 1) 'Runner 队列观察周期有效'
Assert-Condition ($config.runner.worker_launch_enabled -is [bool]) 'Runner AI Worker 启动开关为布尔值'
Assert-Condition ($config.codex_cli.sandbox -eq 'workspace-write') '正式 Codex Worker 使用可写沙箱'
Assert-Condition ($config.codex_cli.planner_sandbox -eq 'read-only') 'Planner Codex Worker 使用只读沙箱'
Assert-Condition ($config.codex_cli.use_user_config -eq $true) '正式 Codex Worker 保留本机 Provider 路由'
Assert-Condition ($config.codex_cli.planner_use_user_config -eq $true) 'Planner Codex Worker 保留本机 Provider 路由'
Assert-Condition ($config.codex_cli.approval_policy -eq 'never') 'Codex Worker 禁止交互式权限提升'
Assert-Condition (($config.codex_cli.disable_features -contains 'plugins') -and ($config.codex_cli.disable_features -contains 'hooks')) 'Codex Worker 禁用用户插件和 Hook'

$operatorPrompt = Read-Utf8Strict -Path (Join-Path $root $config.prompts.operator)
$plannerPrompt = Read-Utf8Strict -Path (Join-Path $root $config.prompts.planner)
$workerPrompt = Read-Utf8Strict -Path (Join-Path $root $config.prompts.worker)
$codexWorkerPrompt = Read-Utf8Strict -Path (Join-Path $root $config.codex_cli.prompt)
$loopctlSource = Read-Utf8Strict -Path (Join-Path $root 'control\loopctl.py')
$plannerControlSource = Read-Utf8Strict -Path (Join-Path $root 'scheduler\planner_control.py')
$schedulerMainSource = Read-Utf8Strict -Path (Join-Path $root 'scheduler\main.py')
$schedulerDispatchSource = Read-Utf8Strict -Path (Join-Path $root 'scheduler\execution_dispatch.py')
$runnerSource = Read-Utf8Strict -Path (Join-Path $root 'runner\agent_runtime.py')
Assert-Condition ($plannerPrompt -match '不负责周期调度') 'Planner Worker 提示词声明非服务边界'
Assert-Condition ($plannerPrompt -match '判断完成任务需要的最终能力等级') 'Planner Worker 负责最终能力判断'
Assert-Condition ($plannerPrompt -match '判断任务是否应拆分') 'Planner Worker 负责拆分建议'
Assert-Condition ($schedulerMainSource -match 'runtime\.write_heartbeat') 'Scheduler 主进程发布 heartbeat'
Assert-Condition ($schedulerMainSource -match 'schedule-preflight') 'Scheduler 主进程通过 loopctl 执行持久排队'
Assert-Condition ($schedulerMainSource -match 'schedule-execution') 'Scheduler 主进程通过 loopctl 执行正式持久排队'
Assert-Condition ($schedulerMainSource -match 'preflight_interval_seconds') 'Scheduler 主进程独立维护预检排队周期'
Assert-Condition ($schedulerMainSource -match 'execution_interval_seconds') 'Scheduler 主进程独立维护正式执行排队周期'
Assert-Condition ($schedulerDispatchSource -match "status='QUEUED'") 'Dispatcher 创建正式 QUEUED execution'
Assert-Condition ($schedulerDispatchSource -notmatch 'subprocess\.Popen|launch_detached_process') 'Dispatcher 不启动 Runner 或 Worker'
Assert-Condition ($schedulerMainSource -notmatch 'planner_runner\.py') 'Scheduler 主进程不直接引用预检 Runner'
Assert-Condition ($runnerSource -match '_launch_ai_workers') 'Runner 统一启动 AI Worker'
Assert-Condition ($runnerSource -match 'execution_kind') 'Runner 按 execution kind 路由 Worker'
Assert-Condition ($operatorPrompt -match 'Planner 阶段状态') 'Operator 提示词声明 Planner 阶段状态'
Assert-Condition (
    ($loopctlSource -match 'from scheduler\.planner_control import') -and
    ($plannerControlSource -match 'command_schedule_preflight')
) 'Planner 预检命令绑定到受控状态机'
Assert-Condition ($plannerControlSource -match 'preflight_executions') 'Planner 控制协议使用预检 execution fencing'
Assert-Condition ($workerPrompt -match 'scope_lock_credential') 'Worker 编辑前核对锁凭证'
Assert-Condition ($workerPrompt -match 'extend-scope') 'Worker 新范围写入前原子扩锁'
Assert-Condition ($workerPrompt -match '唯一允许自动恢复的已跟踪范围外文件') 'Worker 仅允许自动恢复严格证明归属的已跟踪字节码缓存'
Assert-Condition ($workerPrompt -match '不得删除文件、修改索引、使用通配符或递归操作') 'Worker 字节码恢复不得扩大为删除或批量回退权限'
Assert-Condition ($codexWorkerPrompt -match '不调用 `loopctl.py`') 'Codex Worker 不直接操作控制面'
Assert-Condition ($codexWorkerPrompt -match '宿主负责 heartbeat') 'Codex Worker 由宿主管理生命周期'
Assert-Condition ($config.self_hosted_agent.provider_factories.deepseek -eq 'loop_agent.providers.deepseek:create_provider') '内部 Provider 工厂路径已登记'

[ordered]@{
    outcome = 'VALID'
    config = $resolvedConfig
    checks = $script:Checks.Count
    runtime_environments = @('self_hosted_agent', 'codex_cli')
    operator_actions = @(
        '复核 Scheduler 预检排队、正式执行排队、独立周期和 heartbeat 边界。',
        '逐条复核内部运行环境 L1-L5 的模型、reasoning 和 attempt 参数。',
        '确认 Planner 预检控制协议仍通过 loopctl.py 受控。',
        '确认 Scheduler 只执行两类持久排队，Runner 负责容量、路由和 Worker 子进程。'
    )
} | ConvertTo-Json -Depth 5
