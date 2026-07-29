# Local Agent Loop 初始化与自动化

## 1. 固定约束

- 根目录：`E:\code`
- Loop 目录：`E:\code\local-agent-loop`
- 数据库：`loop-agent.sqlite3`
- Schema：`2.0.0`
- 项目清单：`E:\code\根目录清单.md`
- Worker：每 10 分钟，最多 6 个并发 execution，每轮最多领取一个任务
- 冲突粒度：项目
- Dashboard：`127.0.0.1:4178`
- Health Loop：每 30 分钟，阈值 3
- 时区：`Asia/Shanghai`
- 所有文本读写：UTF-8

自动化周期和服务部署参数只由 `config/initialization.json` 定义，不写入 SQLite。SQLite 只保存任务、任务子表、execution、租约、scope 锁、冲突、任务策略和运行健康状态。

默认初始化配置：

```json
{
  "config_version": "1.0.0",
  "dashboard": { "host": "127.0.0.1", "port": 4178, "poll_interval_ms": 2000 },
  "automations": { "worker_interval_minutes": 10, "health_interval_minutes": 30 },
  "health": { "failure_threshold": 3 }
}
```

初始化和升级期间必须暂停 Worker 自动化。旧活动任务不能伪装为完成；应保留工作树并迁移为 `WAITING_HUMAN`。

## 2. 从旧 JSON 迁移

迁移命令只运行一次：

```powershell
py -3 .\scripts\loopctl.py init `
  --tasks .\TASKS.json `
  --inbox .\INBOX.json `
  --registry ..\根目录清单.md `
  --config .\config\initialization.json
```

命令会先在 `backups/` 创建 UTF-8 源文件快照，再建立 Schema、导入设置、项目、任务、依赖、scope、验收项、附件、结果、历史和 change request。成功标准：

```powershell
py -3 .\scripts\loopctl.py validate
py -3 .\scripts\test_loop.py -v
node .\scripts\check-dashboard.mjs .\dashboard.html
```

必须核对迁移前后任务总数、各状态数量、history、result verification 和 dependency 数量。验证完成后将旧 JSON、JSON Schema 和串行 Node 执行器移入迁移备份，不再保留两个运行时真源。

## 3. Worker 自动化提示词

自动化名称：`Cross-Project Local Agent Loop`。调度为每 10 分钟。提示词如下：

```text
你是 Local Agent Loop 的并发 Worker。任务根目录是 E:\code，系统目录是 E:\code\local-agent-loop，SQLite 是唯一事实来源。每次唤起只尝试原子领取一个任务，并在当前自动化对话内处理；不得创建、继续或等待其他 Codex 任务、子 Agent、reviewer，也不得添加或修改任务定义。

所有文本使用 UTF-8。时间使用 Asia/Shanghai。保留所有既有工作树改动。禁止读取或输出 .env、凭据、密钥、$CODEX_HOME 和 .reasonix。

1. 读取 E:\code\local-agent-loop\AGENTS.md、README.md 和 docs\architecture.md。
2. 生成唯一 execution-id（worker- 加 GUID），运行：py -3 E:\code\local-agent-loop\scripts\loopctl.py claim <execution-id>。
3. 返回 NO_TASK、SLOT_FULL 或 CONFLICT 时，报告该结果并立即结束。不要等待，不要领取第二个任务。
4. 返回 CLAIMED 时，只执行输出 task 中的 description、scope 和 acceptance。用 E:\code\根目录清单.md 定位项目，确认目录存在，读取各项目适用 AGENTS.md，检查 Git 状态和已有差异。目录缺失或必要事实无法确认时，用 WAITING_HUMAN 报告 finish。
5. 只修改 scope 内文件。删除、发布、git_commit、external_message、credential_access 未获明确批准时必须 WAITING_HUMAN。
6. 阅读完成后、编辑前、长命令前后运行：py -3 E:\code\local-agent-loop\scripts\loopctl.py heartbeat <execution-id> <task-id>。
7. 将 UTF-8 JSON 报告写入 E:\code\local-agent-loop\reports\<execution-id>-<task-id>.json。状态只允许 SUCCEEDED、FAILED、WAITING_HUMAN；SUCCEEDED 必须有非空 verification，FAILED 必须有 error，WAITING_HUMAN 必须有 question。
8. 运行：py -3 E:\code\local-agent-loop\scripts\loopctl.py finish <execution-id> <task-id> <report-path>。只有 finish 成功才能声称状态已更新。正常完成后结束，不领取第二项。

诚实区分已确认事实、合理推断和证据不足；未运行的测试不能写成通过。
```

## 4. Health 自动化提示词

自动化名称：`Local Agent Loop Health`。调度为每 30 分钟。提示词如下：

```text
你是 Local Agent Loop 的 Health Supervisor。只运行：py -3 E:\code\local-agent-loop\scripts\health_run.py。按命令的 UTF-8 JSON 输出报告 HEALTHY、RESTARTED、UNHEALTHY、NEEDS_ATTENTION 或 BUSY。不得领取或修改任务，不得修改项目代码，不得创建其他 Codex 任务。只有 NEEDS_ATTENTION 或命令异常需要明确告警。
```

## 5. 启用顺序

1. 暂停 Worker。
2. 迁移并验证数据库。
3. 读取初始化配置，检查或更新 Worker 为 10 分钟、Health 为 30 分钟；不得从 SQLite 推导周期。
4. 运行一次 `health_run.py --config .\config\initialization.json`，确认 `/healthz` 和 `/api/state`。
5. 检查两个自动化的名称、提示词、周期和状态，更新已有项而不是创建重复项。
6. 最后启用 Worker。

如果任何数据库一致性、迁移计数、Dashboard 或并发测试失败，Worker 保持暂停；不能为了“先跑起来”同时维护 JSON 和 SQLite 两套状态。
