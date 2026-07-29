# Local Agent Loop

`E:\code\local-agent-loop` 是 `E:\code` 下跨项目任务的并发执行中心。SQLite 是唯一事实来源；Operator 只管理任务，Worker 自动化执行任务，Health Loop 只维护 Dashboard Server。

## 固定配置

- 数据库：`E:\code\local-agent-loop\loop-agent.sqlite3`
- 项目清单：`E:\code\根目录清单.md`
- 最大并发 Worker：6
- Worker 默认调度：每 10 分钟，由 `config/initialization.json` 配置
- scope 冲突：默认按项目加锁
- Dashboard：`http://127.0.0.1:4178`，地址和前端轮询由初始化配置提供
- Health Loop 默认调度：每 30 分钟，连续 3 次恢复失败告警
- 时区：`Asia/Shanghai`
- 文本编码：UTF-8

## 角色

- Operator：当前人工主对话。只通过 `loopctl.py` 添加、修改、取消、重排和确认任务，不执行任务业务内容。
- Worker：每次自动化启动生成唯一 execution id，原子领取一个任务，在当前自动化对话内执行并回写结果。
- Health Loop：只运行 `health_run.py`，检查并按需恢复 Dashboard Server；不领取任务。
- Dashboard Server：直接查询 SQLite 并提供 `/api/state`、`/healthz` 和监控页。

## 常用命令

```powershell
py -3 .\scripts\loopctl.py validate
py -3 .\scripts\loopctl.py state
py -3 .\scripts\loopctl.py enqueue .\new-task.json
py -3 .\scripts\loopctl.py update INIT-001 .\task-patch.json
py -3 .\scripts\loopctl.py requeue TASK-ID --reason "人工确认后重新排队"
py -3 .\scripts\loopctl.py cancel TASK-ID --reason "不再需要"
py -3 .\scripts\loopctl.py confirm TASK-ID --reason "人工复核通过"
```

“删除任务”使用 `cancel`，保留历史审计，不物理删除任务记录。`confirm` 只接受 `SUCCEEDED`，形成 `SUCCEEDED -> CONFIRMED` 的人工归档链路。

Worker 协议：

```powershell
py -3 .\scripts\loopctl.py claim <execution-id>
py -3 .\scripts\loopctl.py heartbeat <execution-id> <task-id>
py -3 .\scripts\loopctl.py finish <execution-id> <task-id> <report.json>
```

`claim` 可能返回 `CLAIMED`、`NO_TASK`、`SLOT_FULL` 或 `CONFLICT`。冲突任务进入 `WAITING_CONFLICT`；阻塞执行结束后自动回到 `PENDING`。Worker 不等待其他任务，也不创建子任务或新的 Codex 对话。

## 文件

- `schemas/loop-agent.sql`：Schema 2.0.0。
- `config/initialization.json`：自动化周期和服务部署配置，不写入 SQLite。
- `scripts/loopdb.py`：SQLite 数据访问和状态投影。
- `scripts/loopctl.py`：任务管理与 Worker 事务协议。
- `scripts/dashboard_server.py`：本地只读 HTTP 服务。
- `scripts/health_run.py`：Dashboard 健康检查和恢复。
- `scripts/test_loop.py`：并发、冲突、租约和确认回归测试。
- `dashboard.html`：通过 `/api/state` 自动轮询，不读取 SQLite 文件、不请求文件授权。
- `backups/`：旧 JSON 系统和迁移前快照，仅用于审计恢复。

详细状态机和锁规则见 `docs/architecture.md`；重建与自动化提示词见 `docs/initialization.md`。
