# Local Agent Loop

Local Agent Loop 是 `E:\code` 下的本地多项目任务控制中心。Operator 管理任务，
Planner 做只读预检，Worker 在取得 scope 锁后执行，Dashboard 展示状态并提供受控操作。

> 本文只供人工快速了解和排障，不是 AI 角色的必读提示词，也不是第二份配置源。
> AI 角色以 `AGENTS.md`、对应角色目录中的提示词、受控入口返回值和目标项目规则为准。

## 快速开始

```powershell
py -3 .\scripts\loopctl.py validate
py -3 .\scripts\loopctl.py state
py -3 .\supervisor\main.py health
py -3 .\supervisor\main.py serve
```

Dashboard 默认地址、端口及其他部署参数以 `config/initialization.json` 为准。
`serve` 用于人工前台运行；计划任务使用 `health` 做探活和必要恢复。

## 工作流程

1. Operator 通过 `loopctl.py` 创建或更新任务；新任务进入 `DRAFT/UNINSPECTED`。
2. Planner 只读检查目标项目，补充精确 scope、能力等级、锁模式和技术验收。
3. 预检通过后任务进入 `PENDING/READY`，由匹配运行环境和能力等级的 Worker 领取。
4. Worker 持有有效 scope 锁后实现并验证，最终写回 `SUCCEEDED`、`FAILED` 或 `WAITING_HUMAN`。
5. 人工复核成功任务后执行 `confirm`；终态任务可独立归档。

任务状态只能通过 `scripts/loopctl.py` 或复用它的受控 Dashboard 操作修改，禁止直接写 SQLite。

## 权威来源

| 信息 | 权威来源 |
| --- | --- |
| 跨角色安全边界和角色入口 | `AGENTS.md` |
| Operator、Planner、Codex Worker 协议 | `operator/operator.md`、`planner/planner.md`、`worker/worker.md` |
| Codex CLI 子进程协议 | `runner/cli-worker.md` |
| 运行环境、模型、周期、并发和部署参数 | `config/initialization.json` |
| 任务、预检、execution、依赖和 scope 锁事实 | `data/loop-agent.sqlite3` |
| Schema 与状态约束 | `schemas/loop-agent.sql`、`scripts/loop_agent/` |
| 项目路由 | 实时读取 `E:\code\根目录清单.md` |
| Dashboard 健康状态 | `runtime/health-state.json` |
| 脚本职责和回归命令 | `scripts/README.md` |

README 只提供人工导航。若说明与配置、任务事实或控制代码冲突，以表中对应权威来源为准；
说明文字不得覆盖上述运行时来源。

## 运行入口

| 场景 | 稳定入口 |
| --- | --- |
| 任务管理和状态迁移 | `scripts/loopctl.py` |
| 数据库公共 API 兼容门面 | `scripts/loopdb.py` |
| Codex 客户端自动化 | `worker/worker.md` 与受控 `loopctl.py claim/heartbeat/finish` |
| Codex CLI 周期调度 | `dispatcher/codex_cli_dispatcher.py` |
| Codex CLI 单任务运行 | `runner/codex_cli_runner.py` |
| Self-hosted Agent | `runner/agent_runtime.py` |
| DeepSeek Provider 适配 | `scripts/loop_agent/providers/deepseek.py` |
| Dashboard 健康与前台服务 | `supervisor/main.py`、`client/dashboard_server.py` |
| Supervisor 健康检查实现 | `supervisor/health_run.py` |

各运行环境只领取与自身路由、能力等级和执行策略匹配的 READY 任务。具体模型、超时、
重试、并发和周期不在本文复制，统一读取初始化配置。

## 常用任务命令

```powershell
py -3 .\scripts\loopctl.py enqueue .\new-task.json
py -3 .\scripts\loopctl.py update TASK-ID .\task-patch.json
py -3 .\scripts\loopctl.py requeue TASK-ID --reason "重新预检或执行"
py -3 .\scripts\loopctl.py cancel TASK-ID --reason "不再需要"
py -3 .\scripts\loopctl.py confirm TASK-ID --reason "人工复核通过"
py -3 .\scripts\loopctl.py archive TASK-ID --reason "终态任务归档"
py -3 .\scripts\loopctl.py unarchive TASK-ID --reason "取消归档"
py -3 .\scripts\loopctl.py resolve-human TASK-ID --response "人工答复"
py -3 .\scripts\loopctl.py recover EXECUTION-ID --human-confirmed-safe --action requeue
```

`cancel` 保留审计历史，不物理删除任务。`migrate` 只升级已有 SQLite Schema；
`migrate-legacy` 只用于显式导入旧 `TASKS.json` 和 `INBOX.json`。

## 回归测试

```powershell
$env:PYTHONUTF8 = '1'
py -3 -m unittest discover -s scripts/tests -p "test_*.py"
py -3 .\scripts\loopctl.py validate
```

控制面测试的职责拆分、专项命令和部署检查见 `scripts/README.md`。

## 目录导航

| 路径 | 内容 |
| --- | --- |
| `AGENTS.md` | 跨角色稳定边界 |
| `operator/` | Operator 提示词、任务控制状态机和密钥管理入口 |
| `planner/` | Planner 提示词与只读预检状态机 |
| `worker/` | Worker 提示词与任务执行状态机 |
| `dispatcher/` | Codex CLI 单次调度入口 |
| `runner/` | Codex CLI 与 Self-hosted Runner 入口及 CLI 提示词 |
| `config/initialization.json` | 唯一部署配置源 |
| `schemas/loop-agent.sql` | 当前数据库 Schema |
| `scripts/loopctl.py` | 任务控制 CLI |
| `scripts/loop_agent/` | 控制面、数据库、运行时、Provider 和 Dashboard 内部实现 |
| `supervisor/` | Supervisor 主进程、健康检查与 Windows 计划任务安装器 |
| `scripts/tests/` | Python 回归测试 |
| `client/` | Dashboard 前端静态资源与本机 HTTP/API 服务 |
| `runtime/` | PID、日志和健康状态，不是任务事实源 |
| `data/loop-agent.sqlite3` | 唯一任务事实源 |

## 排障顺序

1. 运行 `py -3 scripts/loopctl.py validate` 检查任务库和配置一致性。
2. 查看 `runtime/health-state.json`，必要时运行 Supervisor `health`。
3. 根据失败角色读取对应角色目录中的提示词和 `scripts/README.md` 的职责导航。
4. 使用 `loopctl.py state` 或 Dashboard 查看任务、execution、依赖和 scope 阻塞事实。

敏感值不进入 README、任务数据库、配置文件或日志。SecretStore 的人工入口是
`operator/secretctl.py`，具体安全约束以 `AGENTS.md`、角色提示词和实现为准。
