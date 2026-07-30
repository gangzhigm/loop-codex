# Local Agent Loop 架构

## 数据边界

```text
Operator / Worker ---> data/loop-agent.sqlite3 <--- Dashboard Server ---> dashboard.html
                              ^                         ^
                              |                         |
                 config/initialization.json    runtime/health-state.json
                              ^                         ^
                              |                         |
                 Worker / 健康任务配置       Windows 任务计划程序

E:\code\根目录清单.md --实时解析项目路由--> loopctl / Dashboard Server
```

SQLite 只包含任务及其执行一致性表：`tasks`、7 张任务子表、`executions`、`scope_locks` 和 `task_conflicts`。它不保存 metadata、settings、projects、change requests、health events 或 service state。

Worker 周期、健康任务周期、租约、并发数、Dashboard 地址和健康阈值只存在于 `config/initialization.json`。项目路由实时读取 `E:\code\根目录清单.md`。健康检查的当前状态和最近事件写入 `runtime/health-state.json`。浏览器不直接读取 SQLite，而是访问本机 HTTP 服务。

## 并发模型

每个 Worker 唤起后只调用一次 `claim`。一个 `BEGIN IMMEDIATE` 事务完成过期恢复、并发名额检查、候选选择、scope 冲突检测、execution 创建、scope 加锁和任务转为 `RUNNING`。

系统默认最多允许 6 个活动 execution。第 7 个领取返回 `SLOT_FULL`。scope 默认归一到项目：

```text
rs/rs-mall4pc-pro/src/views/cart/index.vue -> project:rs/rs-mall4pc-pro
holding/frontend/src/App.tsx               -> project:holding
OSS:bucket/path/file.xlsx                  -> external:OSS:bucket/path/file.xlsx
```

同一项目任务默认互斥。`claim` 按队列顺序扫描依赖就绪任务；冲突任务保存 blocker 信息并进入 `WAITING_CONFLICT`，随后继续寻找其他 scope 可执行的任务。只有所有依赖就绪候选都冲突时，本轮 Worker 才返回 `CONFLICT`。阻塞 execution 完成、心跳超时或租约过期后，任务自动回到 `PENDING`。

## 状态机

```text
DRAFT --人工重排--> PENDING --领取--> RUNNING --> SUCCEEDED --人工复核--> CONFIRMED
                       |                  |  |
                       |                  |  +--> FAILED
                       |                  +-----> WAITING_HUMAN --人工重排--> PENDING
                       +--冲突--> WAITING_CONFLICT --冲突解除--> PENDING

RUNNING --租约过期且仍可重试--> PENDING
RUNNING --租约过期且达到上限--> FAILED
非 RUNNING 状态 --人工取消--> CANCELLED
```

依赖只有在上游为 `SUCCEEDED` 或 `CONFIRMED` 时满足。自动执行结果只允许 `SUCCEEDED`、`FAILED`、`WAITING_HUMAN`；`CONFIRMED` 只能人工产生。

## 顺序与租约

候选按 `critical`、`high`、`medium`、`low`，再按 `created_at` 和 id 排序。仅 `PENDING` 且依赖完成的任务可领取。`NO_TASK`、`SLOT_FULL` 或 `CONFLICT` 都应立即结束。

默认心跳超时 300 秒、租约 3600 秒。Worker 在阅读完成后、编辑前以及长命令前后调用 `heartbeat`。后续 `claim` 会回收心跳超时或租约过期的 execution，释放其 scope 锁，并按最大尝试次数重排或失败。正常结束将 UTF-8 JSON 通过 stdin 交给 `finish`；`finish` 在同一事务中保存结果、释放 scope 锁并重新排队已解除冲突的任务，不生成 report 文件。

## 安全边界

scope 必须相对 `E:\code` 并匹配项目清单中最长的项目路径。绝对路径、`..`、`$CODEX_HOME`、`.reasonix`、`.env` 和未登记项目会被拒绝。Worker 还必须读取目标项目适用的 `AGENTS.md`、检查 Git 工作树、保留既有改动并只处理已领取 scope。

删除、发布、Git 提交、外部消息和凭据访问需要明确人工授权；授权应体现在当前任务内容或 Operator 的明确指令中。缺少授权时以 `WAITING_HUMAN` 完成本轮。

## 服务健康

Dashboard Server 默认绑定 `127.0.0.1:4178`：

- `/`：监控页。
- `/api/state`：合并任务库、初始化配置、项目清单和运行时健康状态。
- `/healthz`：Schema、任务数和活动 execution 健康信息。

Windows 任务计划程序默认每 10 分钟直接运行一次 `health_run.py`，不调用 Codex 模型。服务正常时写入 `HEALTHY`；不可用时尝试恢复；连续达到阈值后写入 `NEEDS_ATTENTION`。健康状态保存在 `runtime/health-state.json`，独立短时锁避免健康任务重入。
