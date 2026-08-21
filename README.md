# Local Agent Loop

Local Agent Loop 是 `E:\code` 下的本地多项目任务控制中心。Operator 管理任务，
Worker 在取得 scope 锁后执行，Dashboard 展示状态并提供受控操作。Scheduler 在一个常驻
进程内独立安排 Planner 预检排队和 READY 任务正式执行排队；Runner 作为独立常驻 AI 管理器
读取两类队列、计算容量，并按 execution 类型、运行环境和能力等级启动 AI Worker。
系统仅支持 Windows，进程管理、计划任务和系统密钥存储均直接使用 Windows 能力。

> 本文只供人工快速了解和排障，不是 AI 角色的必读提示词，也不是第二份配置源。
> AI 角色以 `AGENTS.md`、对应角色目录中的提示词、受控入口返回值和目标项目规则为准。

## 快速开始

```powershell
py -3 .\control\loopctl.py validate
py -3 .\control\loopctl.py state
py -3 .\supervisor\health_run.py
py -3 .\supervisor\main.py serve
py -3 .\client\dashboard_server.py
```

Dashboard 默认地址、端口及其他部署参数以 `config/initialization.json` 为准。
Supervisor `serve` 用于人工前台运行，周期检查并恢复独立的 Dashboard、Scheduler 和 Runner；
Dashboard 也可通过 `client/dashboard_server.py` 单独启动，停止 Supervisor 不会停止 Dashboard；
Windows 计划任务通过 `supervisor/run.ps1` 调用 `health_run.py` 做探活和必要恢复。

## 工作流程

1. Operator 通过 `loopctl.py` 创建或更新任务；新任务进入 `DRAFT/UNINSPECTED`。
2. Scheduler 周期选择 `DRAFT/UNINSPECTED` 并通过 `loopctl.py` 原子排入 Planner 预检队列。
3. Scheduler 另一条周期链选择 `PENDING/READY` 自动任务，原子排入 `QUEUED/READY` 并创建 `WORKER/QUEUED` execution。
4. Runner 读取 `PLANNER/QUEUED`，启动只读 Codex Planner Worker并写回最终能力、范围、冲突判断或拆分建议。
5. Runner 读取 `WORKER/QUEUED`，按 execution 固化档位启动 Self-hosted 或 Codex CLI Worker，Worker 原子转入 `RUNNING` 并完成任务。
6. 人工复核成功任务后执行 `confirm`；终态任务可独立归档。

任务状态只能通过 `control/loopctl.py` 或复用它的受控 Dashboard 操作修改，禁止直接写 SQLite。

## 权威来源

| 信息 | 权威来源 |
| --- | --- |
| 跨角色安全边界和角色入口 | `AGENTS.md` |
| Operator、Planner 说明、通用 Worker 协议 | `operator/operator.md`、`scheduler/planner.md`、`worker/worker.md` |
| 内部 Agent Worker 协议 | `worker/worker.md`、`worker/agent_runtime.py` |
| 运行环境、模型、周期、并发和部署参数 | `config/initialization.json` |
| 任务、预检、execution、依赖和 scope 锁事实 | `data/loop-agent.sqlite3` |
| Schema 与状态约束 | `schemas/loop-agent.sql`、`control/loop_agent/` |
| 项目路由 | 实时读取 `E:\code\根目录清单.md` |
| Dashboard 汇总健康状态 | `data/runtime/health-state.json` |
| Runner 服务 heartbeat 与队列快照 | `data/runtime/runner-heartbeat.json`、`data/runtime/runner-queue-state.json` |
| 脚本职责和回归命令 | `control/README.md` |

README 只提供人工导航。若说明与配置、任务事实或控制代码冲突，以表中对应权威来源为准；
说明文字不得覆盖上述运行时来源。

## 运行入口

| 场景 | 稳定入口 |
| --- | --- |
| 任务管理和状态迁移 | `control/loopctl.py` |
| 数据库公共 API | `control/loopdb.py` |
| 通用 Worker 角色协议 | `worker/worker.md` |
| Scheduler 常驻双排队服务 | `scheduler/main.py` 的 `serve` 命令 |
| Scheduler 内部正式执行排队 | `scheduler/execution_dispatch.py` |
| Scheduler 内部 Planner 预检状态机 | `scheduler/planner_control.py` |
| Runner 常驻 AI 队列管理 | `runner/agent_runtime.py` 的 `serve` 命令 |
| 内部 Agent 单任务运行 | `worker/agent_runtime.py` |
| Codex CLI 正式 Worker | `worker/codex_cli_runtime.py` |
| Codex CLI Planner Worker | `worker/planner_codex_runtime.py` |
| DeepSeek Provider 适配 | `control/loop_agent/providers/deepseek.py` |
| Dashboard 独立 HTTP 服务 | `client/dashboard_server.py` |
| Dashboard、Scheduler 与 Runner 进程监控 | `supervisor/main.py` |
| Supervisor 健康检查实现 | `supervisor/health_run.py` |

各运行环境只领取与自身路由、能力等级和执行策略匹配的 READY 任务。具体模型、超时、
重试、并发和周期不在本文复制，统一读取初始化配置。

## 常用任务命令

```powershell
py -3 .\control\loopctl.py enqueue .\new-task.json
py -3 .\control\loopctl.py update TASK-ID .\task-patch.json
py -3 .\control\loopctl.py requeue TASK-ID --reason "重新预检或执行"
py -3 .\control\loopctl.py cancel TASK-ID --reason "不再需要"
py -3 .\control\loopctl.py confirm TASK-ID --reason "人工复核通过"
py -3 .\control\loopctl.py archive TASK-ID --reason "终态任务归档"
py -3 .\control\loopctl.py unarchive TASK-ID --reason "取消归档"
py -3 .\control\loopctl.py resolve-human TASK-ID --response "人工答复"
py -3 .\control\loopctl.py recover EXECUTION-ID --runner-confirmed-terminated --action requeue
```

`cancel` 保留审计历史，不物理删除任务。`migrate` 只升级已有 SQLite Schema，
并保留数据库中的任务、执行、锁和审计数据。

## 回归测试

```powershell
$env:PYTHONUTF8 = '1'
py -3 -m unittest discover -s tests -p "test_*.py"
py -3 .\control\loopctl.py validate
```

控制面测试的职责拆分、专项命令和部署检查见 `control/README.md`。

Dashboard 使用 React、TypeScript 和 Vite，源码与开发说明位于 `client/frontend/`；
Python 服务直接提供已提交的 `client/dist/`，生产启动不运行 npm 或加载 CDN。

## 目录导航

| 路径 | 内容 |
| --- | --- |
| `AGENTS.md` | 跨角色稳定边界 |
| `operator/` | Operator 提示词、任务控制状态机和密钥管理入口 |
| `scheduler/` | 常驻调度服务、Planner 预检协议/状态机与正式执行排队 |
| `worker/` | Worker 提示词与任务执行状态机 |
| `runner/` | 常驻 AI 队列、容量、路由和 Worker 子进程管理器 |
| `config/initialization.json` | 唯一部署配置源 |
| `schemas/loop-agent.sql` | 当前数据库 Schema |
| `control/loopctl.py` | 任务控制 CLI |
| `control/loop_agent/` | 控制面、数据库、运行时、Provider 和 Dashboard 内部实现 |
| `supervisor/` | Supervisor 主进程、健康检查与 Windows 计划任务安装器 |
| `common/service_runtime.py` | Supervisor、Dashboard、Scheduler、Runner 共用的运行文件生命周期 |
| `tests/` | Python 回归测试 |
| `client/frontend/` | React、TypeScript 和 Vite Dashboard 源码 |
| `client/dist/` | Dashboard 可直接部署的本地静态产物 |
| `client/dashboard_server.py` | Dashboard 本机 HTTP/API 与静态资源服务 |
| `data/` | SQLite、任务附件、备份、PID、日志和健康状态 |
| `data/loop-agent.sqlite3` | 唯一任务事实源 |
| `data/assets/` | 任务附件目录 |
| `data/backups/` | 数据库迁移审计与灾难恢复快照 |
| `data/runtime/` | PID、日志和健康状态，不是任务事实源 |

## 排障顺序

1. 运行 `py -3 control/loopctl.py validate` 检查任务库和配置一致性。
2. 查看 `data/runtime/supervisor-heartbeat.json` 和 `data/runtime/health-state.json`，必要时运行 `supervisor/health_run.py`。
3. 根据失败角色读取对应角色目录中的提示词和 `control/README.md` 的职责导航。
4. 使用 `loopctl.py state` 或 Dashboard 查看任务、execution、依赖和 scope 阻塞事实。

敏感值不进入 README、任务数据库、配置文件或日志。SecretStore 的人工入口是 `operator/secretctl.py`，具体安全约束以 `AGENTS.md`、角色提示词和实现为准。
