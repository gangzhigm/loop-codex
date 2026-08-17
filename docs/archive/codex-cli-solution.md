# Codex CLI 方案归档

归档时间：2026-08-17  
最后使用的初始化配置版本：`4.5.0`  
状态：已退出活动系统，仅用于说明历史架构和数据库历史值来源。

## 归档范围

本方案曾把 Codex CLI 作为 Local Agent Loop 的一种内部执行环境，环境标识为
`codex_cli`。系统内有两条独立路径：Planner 静态预检和 Worker 任务执行。两条路径都
由宿主 Runner 管理控制面状态，模型进程不能直接领取任务或写任务数据库。

归档前的主要文件如下：

| 文件 | 历史职责 |
| --- | --- |
| `dispatcher/codex_cli_dispatcher.py` | 选择一个匹配的自动任务并启动 Worker Runner |
| `dispatcher/main.py` | 常驻调度器，周期调用单轮 Dispatcher |
| `runner/codex_cli_runner.py` | Worker 的 claim、Codex 子进程、heartbeat、超时和 finish |
| `runner/planner_runner.py` | Planner 的 preflight claim、只读 Codex 子进程和预检写回 |
| `runner/cli-worker.md` | Codex CLI Worker 的任务边界和最终结果协议 |
| `common/codex.py` | 输出缓冲、诊断脱敏、JSONL 解析和进程树终止 |
| `config/initialization.json` | CLI 模型档位、沙箱、重试、超时和 Dispatcher 配置 |

源码本身不复制进归档目录，避免历史实现重新进入活动导入路径；需要核对精确实现时使用
Git 历史。

## Worker 执行链

```text
Dispatcher Scheduler
  -> 单轮 Codex CLI Dispatcher
  -> 只读任务/活动 execution 快照
  -> 选择 runtime_environment=codex_cli 的 PENDING/READY 自动任务
  -> 检查全局和平台并发容量
  -> 启动 codex_cli_runner.py 后立即放手
  -> Runner 通过 loopctl 原子 claim
  -> Runner 启动 codex exec
  -> Codex 在任务 scope 内执行并输出 JSONL
  -> Runner 校验最终报告
  -> Runner 通过 loopctl finish
  -> Runner 退出
```

Dispatcher 的候选选择不是任务锁。最终是否取得任务始终由 Runner 调用 `loopctl claim`
决定，因此多个调度轮次看到同一候选时，只有一个 Runner 能取得 execution 和 scope 锁。

Worker Runner 按能力等级解析不可变 execution profile，包括模型、推理等级、单次超时和
重试次数。它把 `runner/cli-worker.md` 与已领取任务上下文送入 `codex exec`，并要求最终
结果符合 `SUCCEEDED / FAILED / WAITING_HUMAN` 控制面协议。

## Planner 执行链

```text
Planner Scheduler
  -> 启动一次性 planner_runner.py 后立即放手
  -> Runner 通过 preflight-claim 预留一个 DRAFT/UNINSPECTED 任务
  -> 启动只读、无批准、无用户配置的 codex exec
  -> Codex 静态检查任务定义和项目文件
  -> Runner 校验 READY / NEEDS_REVIEW / FAILED 结果
  -> Runner 通过对应 preflight 命令写回
  -> Runner 退出
```

Planner 的 Codex 进程使用 `read-only` 沙箱、`approval_policy=never`、临时输出 Schema 和
ephemeral 会话。Runner 负责 preflight heartbeat 和 row-version fencing，模型不能调用
`loopctl.py`，也不能直接修改文件、数据库或调度器。

## 进程和失败处理

- CLI 子进程通过参数数组启动，不经过 shell 解析。
- stdout/stderr 由独立线程持续读取，并只保留配置上限内的尾部，防止长期任务占满内存。
- Worker 从 Codex JSONL 的 Agent 消息中提取最终 JSON；Planner 使用临时 JSON Schema
  约束输出。
- attempt 超时后按精确 PID 终止 Windows 进程树；Runner 只处理自己创建的进程。
- 只有确认没有本地副作用的失败才能重试；可能已经写入后不重放整个 attempt。
- 公开诊断会移除认证头、密钥样式文本和私有工具目录，不记录提示词、推理和文件内容。
- Codex 登录、账户或模型权限故障归类为宿主依赖故障，不由任务代码修复。

## 监控协议

每个 Planner/Worker Runner 曾在 `runtime/runners/` 写入原子状态文件，包含 Runner ID、
模式、环境、execution ID、任务 ID、Runner PID、Codex 子进程 PID 和 heartbeat 时间。
Supervisor 只读汇总这些状态，不终止、不恢复、不删除 Runner，也不修改任务状态。

Dispatcher Scheduler 和 Planner Scheduler 各自维护 PID、heartbeat 和停止请求文件，由
Supervisor 根据配置开关管理。已经启动的 Runner 与 Scheduler 解耦，关闭 Scheduler 只会
阻止后续分发。

## 历史配置契约

`4.5.0` 配置曾包含：

- `runtime_environments.codex_cli`；
- `execution_profiles.codex_cli` 的 L1-L5 模型、推理、超时和重试；
- `task_execution.platform_max_active_executions.codex_cli`；
- `planner.default_runtime_environment=codex_cli`；
- `codex_cli.executable`、提示词、沙箱、输出上限和终止宽限；
- `codex_cli.dispatcher` 的周期、日志、工作目录和能力等级。

这些字段在 `5.0.0` 活动配置中被移除。当前配置只登记 `self_hosted_agent`，由明确的
`provider_id` 和 Provider 工厂完成 Planner 与 Worker 调用。

## 数据库历史值

SQLite 历史记录可能仍包含 `codex_cli`。该值只为保留既有任务和 execution 审计数据，
不是当前可选、可领取或可调度的运行环境。活动控制面不得为新任务创建该值，也不得把
历史 CLI 任务自动改写成内部 Agent 任务；需要重新执行时，应由 Operator 通过受控状态
流程明确选择当前 `self_hosted_agent` 路由。

## 退出后的替代关系

| 历史组件 | 当前组件 |
| --- | --- |
| Codex CLI Worker Runner | `runner/agent_runtime.py` |
| Codex CLI Dispatcher | `dispatcher/agent_dispatcher.py` |
| Codex CLI Planner 子进程 | `runner/planner_runner.py` 的内部 Provider 工具循环 |
| CLI 模型档位 | `execution_profiles.self_hosted_agent.providers` |
| CLI 登录与账户状态 | Provider + SecretStore 边界 |

退出 CLI 方案不改变 Planner、Dispatcher、Runner 和 Supervisor 的职责：Scheduler 仍只
启动一次性 Runner，Runner 仍拥有任务生命周期，Supervisor 仍只管理常驻 Scheduler 并
观察动态 Runner。
