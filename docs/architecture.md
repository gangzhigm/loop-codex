# Local Agent Loop 架构

## 数据流与角色边界

```text
Operator -- enqueue/update/requeue/cancel/confirm --+
                                                   |
Worker automation -- claim/heartbeat/finish -------+--> loop-agent.sqlite3
                                                   |           |
Health automation -- health_run.py ----------------+           v
                                                     Dashboard Server --> dashboard.html
```

SQLite 是任务及其执行一致性数据的唯一事实来源。根目录不保留运行中的 `TASKS.json`、`INBOX.json` 或全局 JSON 锁。Dashboard 不能直接读取 SQLite 文件；浏览器只访问本机 HTTP 服务，由服务在每个请求中只读查询数据库。

自动化周期和服务部署参数不属于任务数据，只存在于 `config/initialization.json`。其中包括 Worker/Health 周期、Dashboard host/port/轮询周期和健康失败阈值。运行脚本不得从 SQLite `settings` 读取这些值。

## 并发模型

每个 Worker 唤起后只调用一次 `claim`。`BEGIN IMMEDIATE` 将过期恢复、并发名额检查、候选选择、scope 冲突检测、execution 创建、scope 加锁和任务转为 `RUNNING` 放在一个事务中。

系统允许最多 6 个 `RUNNING` execution。第 7 个领取返回 `SLOT_FULL` 并结束。一个任务只允许一个活动 execution。

scope 默认归一到其所属项目，例如：

```text
rs/rs-mall4pc-pro/src/views/cart/index.vue -> project:rs/rs-mall4pc-pro
holding/frontend/src/App.tsx               -> project:holding
OSS:bucket/path/file.xlsx                  -> external:OSS:bucket/path/file.xlsx
```

因此同一项目的两个任务默认互斥，即使文件不同；涉及多个项目的任务必须同时取得全部项目锁。检测到冲突时，后来的任务转为 `WAITING_CONFLICT`，保存 blocker execution 和 scope，不进入工作阶段。阻塞 execution 完成或租约过期后，冲突任务自动转回 `PENDING`。

全局缺失项目不会阻塞其他项目领取。Worker 领取到 scope 所属项目不存在时，应以 `WAITING_HUMAN` 完成本轮并报告缺失路径。

## 状态机

```text
DRAFT -- operator requeue -----------> PENDING
                                         |
                                         v
WAITING_CONFLICT <--- scope conflict -- RUNNING ---> SUCCEEDED ---> CONFIRMED
       |                                 |   |             人工复核
       +-- blocker finished --> PENDING  |   +-----------> FAILED
                                         +---------------> WAITING_HUMAN --> PENDING

RUNNING -- lease expired, attempts left --> PENDING
RUNNING -- lease expired, max attempts ---> FAILED
任意非 RUNNING 状态 -- operator cancel ---> CANCELLED
```

依赖只有在上游为 `SUCCEEDED` 或 `CONFIRMED` 时满足。自动执行结果只允许 `SUCCEEDED`、`FAILED`、`WAITING_HUMAN`。`CONFIRMED` 只能由 Operator 在人工复核后生成。

## 任务顺序

可领取候选按 `critical`、`high`、`medium`、`low`，再按 `created_at` 和 id 排序。仅 `PENDING` 且依赖完成的任务参与选择。一次自动化调用最多领取一个任务；`NO_TASK`、`SLOT_FULL` 或 `CONFLICT` 都应立即结束。

## 租约与恢复

默认租约 3600 秒。Worker 在阅读完成后、编辑前以及长命令前后调用 `heartbeat`。领取命令会先回收过期 execution、释放其 scope 锁，并按 `max_attempts` 将任务重排或失败。

execution 异常退出时不手工伪造结果；租约恢复负责收敛。正常执行必须以结构化 UTF-8 JSON 报告调用 `finish`，由同一事务释放 scope 锁并重新排队已解除的冲突任务。

## 安全边界

scope 必须相对 `E:\code` 并匹配 `根目录清单.md` 中最长的项目路径。绝对路径、`..`、`$CODEX_HOME`、`.reasonix`、`.env` 和未登记项目会被控制器拒绝。Worker 仍须读取每个目标项目适用的 `AGENTS.md`、检查 Git 工作树、保留既有改动，并只处理已领取任务的 scope。

删除、发布、Git 提交、外部消息和凭据访问需要明确人工批准。缺少批准时 Worker 应进入 `WAITING_HUMAN`，不得扩大授权。

## 服务健康

Dashboard Server 绑定 `127.0.0.1:4178`：

- `/`：监控页。
- `/api/state`：SQLite 状态投影。
- `/healthz`：Schema、任务数和活动 execution 健康信息。

Health Loop 默认每 30 分钟运行一次。服务健康时记录 `HEALTHY`；不可用时启动或恢复服务；连续 3 次恢复失败记录 `NEEDS_ATTENTION`。Health Loop 使用独立短时文件锁避免自身重入，但不参与任务 scope 锁。
