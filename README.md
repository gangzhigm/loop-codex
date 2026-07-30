# Local Agent Loop

`E:\code\local-agent-loop` 是 `E:\code` 下跨项目任务的并发执行中心。Operator 管理任务，Codex Worker 执行任务，Windows 健康任务维护 Dashboard Server。

## 固定配置

- 任务数据库：`E:\code\local-agent-loop\data\loop-agent.sqlite3`（Schema 3.0.0）
- 初始化配置：`config/initialization.json`
- 项目清单：`E:\code\根目录清单.md`
- Worker：默认每 10 分钟唤起一次，最多 6 个活动 execution
- Windows 健康任务：默认每 10 分钟运行一次，连续 3 次恢复失败告警
- scope 冲突：默认按项目加锁
- Dashboard：`http://127.0.0.1:4178`
- 时区：`Asia/Shanghai`
- 文本编码：UTF-8

SQLite 只保存任务及其执行一致性数据：任务内容和历史、execution、租约、scope 锁与任务冲突。自动化周期、并发参数和服务部署配置只在 `config/initialization.json`；项目清单实时读取；服务健康状态只在 `runtime/health-state.json`。

## 角色

- Operator：人工主对话，只添加、修改、取消、重排和确认任务。
- Worker：每次唤起原子领取一个任务，在当前自动化任务中执行并回写结果。
- Windows 健康任务：由任务计划程序直接运行 `health_run.py`，检查并按需恢复 Dashboard Server，不调用模型。
- Dashboard Server：读取任务库、初始化配置和运行时健康 JSON，提供监控接口和页面。

## 常用命令

```powershell
py -3 .\scripts\loopctl.py validate
py -3 .\scripts\loopctl.py state
py -3 .\scripts\loopctl.py enqueue .\new-task.json
py -3 .\scripts\loopctl.py update INIT-001 .\task-patch.json
py -3 .\scripts\loopctl.py requeue TASK-ID --reason "人工确认或重新打开后排队"
py -3 .\scripts\loopctl.py cancel TASK-ID --reason "不再需要"
py -3 .\scripts\loopctl.py confirm TASK-ID --reason "人工复核通过"
```

`cancel` 保留历史，不物理删除任务。`requeue` 可重新排队草稿、等待、失败或成功任务；`confirm` 只接受 `SUCCEEDED`，形成 `SUCCEEDED -> CONFIRMED` 的人工归档链路。

Worker 协议：

```powershell
py -3 .\scripts\loopctl.py claim <execution-id>
py -3 .\scripts\loopctl.py heartbeat <execution-id> <task-id>
$resultJson | py -3 .\scripts\loopctl.py finish <execution-id> <task-id> -
```

`finish` 默认从 stdin 读取 UTF-8 JSON，也兼容显式 JSON 文件路径。正常流程不持久化中间 report。`claim` 会把冲突候选转为 `WAITING_CONFLICT` 后继续寻找其他 scope 的任务，并回收心跳超时或租约过期的 execution。它可能返回 `CLAIMED`、`NO_TASK`、`SLOT_FULL` 或 `CONFLICT`；除 `CLAIMED` 外均立即结束。

## 文件

- `data/loop-agent.sqlite3`：唯一任务事实源。
- `schemas/loop-agent.sql`：Schema 3.0.0。
- `config/initialization.json`：执行、自动化与服务配置。
- `prompts/operator.md`：任务管理主对话提示词和查重、状态迁移流程。
- `prompts/worker.md`：Codex Worker 自动化的权威提示词。
- `scripts/loopdb.py`：任务库访问与状态投影。
- `scripts/loopctl.py`：任务管理与 Worker 事务协议。
- `scripts/dashboard_server.py`：本地只读 HTTP 服务。
- `scripts/health_run.py`：Dashboard 健康检查和恢复。
- `scripts/install_health_task.ps1`：按初始化配置注册或更新 Windows 健康任务。
- `scripts/test_loop.py`：并发、冲突、租约和确认回归测试。
- `dashboard.html`：监控页面模板。
- `runtime/`：PID、日志、健康状态和短时锁；不是任务事实源。
- `backups/`：迁移前快照和旧产物，仅用于审计恢复。

详细规则见 `docs/architecture.md`；初始化、Worker 提示词和健康任务安装见 `docs/initialization.md`。
