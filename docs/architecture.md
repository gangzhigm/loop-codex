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

SQLite 只包含任务及其执行一致性表：`tasks`（包括 `execution_profile` 和独立的 nullable `archived_at`）、7 张任务子表、`executions`、`scope_locks` 和 `task_conflicts`。它不保存模型映射、自动化周期、metadata、settings、projects、change requests、health events 或 service state。

Worker 周期、健康任务周期、租约、并发数、Dashboard 地址和健康阈值只存在于 `config/initialization.json`。项目路由实时读取 `E:\code\根目录清单.md`。健康检查的当前状态和最近事件写入 `runtime/health-state.json`。浏览器不直接读取 SQLite，而是访问本机 HTTP 服务。

## 并发模型

每个 Worker 唤起后只使用自身 `execution_profile` 调用一次 `claim`。一个 `BEGIN IMMEDIATE` 事务完成过期恢复、全局与档位名额检查、同档位候选选择、scope 冲突检测、execution 创建、scope 加锁和任务转为 `RUNNING`。

系统默认最多允许 6 个活动 execution；各档位上限分别为 `routine=2`、`standard=3`、`advanced=2`、`deep=1`、`complex=1`、`exceptional=1`。两个限制同时生效，档位上限之和不代表全局容量。任一限制达到时领取返回 `SLOT_FULL`，并通过 `limit_scope` 区分全局或档位。scope 默认归一到项目：

```text
rs/rs-mall4pc-pro/src/views/cart/index.vue -> project:rs/rs-mall4pc-pro
holding/frontend/src/App.tsx               -> project:holding
OSS:bucket/path/file.xlsx                  -> external:OSS:bucket/path/file.xlsx
```

同一项目任务默认互斥。`claim` 在当前档位内按队列顺序扫描依赖就绪任务；冲突任务保存 blocker 信息并进入 `WAITING_CONFLICT`，随后继续寻找当前档位其他 scope 可执行的任务。只有所有依赖就绪候选都冲突时，本轮 Worker 才返回 `CONFLICT`。阻塞 execution 完成、心跳超时或租约过期后，任务自动回到 `PENDING`。

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

归档不属于状态机。`archived_at IS NULL` 表示未归档，带 Asia/Shanghai 时区的 ISO 8601 时间表示已归档。`CONFIRMED` 只表示人工复核通过，不会隐式写入 `archived_at`。人工 `archive/unarchive` 只修改该属性，并以原状态到同一原状态的管理事件记录 actor、时间和 reason；允许归档的终态为 `CONFIRMED`、`FAILED` 和 `CANCELLED`。

## 顺序与租约

候选在各自执行档位内按 `blocker`、`critical`、`high`、`medium`、`low`，再按 `created_at` 和 id 排序。仅 `PENDING` 且依赖完成的任务可领取。优先级和执行档位相互独立，高优先级不会自动升高模型档位。`NO_TASK`、`SLOT_FULL` 或 `CONFLICT` 都应立即结束。

五个普通档位各由一条 Codex 定时自动化驱动，默认每 10 分钟错峰运行；`exceptional` 没有定时自动化，只能由 Operator 在人工明确批准后创建一次性执行。Codex 自动化不能可靠地暂停或恢复自身或其他自动化，因此本阶段不做无任务自动暂停；`NO_TASK` 只结束当前轮次，Worker 不读取或修改自动化状态。

默认心跳超时 300 秒、租约 3600 秒。Worker 在阅读完成后、编辑前以及长命令前后调用 `heartbeat`。后续 `claim` 会回收心跳超时或租约过期的 execution，释放其 scope 锁，并按最大尝试次数重排或失败。正常结束将 UTF-8 JSON 通过 stdin 交给 `finish`；`finish` 在同一事务中保存结果、释放 scope 锁并重新排队已解除冲突的任务，不生成 report 文件，也不自动归档任务。

Schema 3.0.0 或 3.1.0 到 3.2.0 的迁移使用受控的 `loopctl.py migrate`。迁移在无活动 execution 时重建任务表以支持 `execution_profile` 和五级优先级，现有任务统一回填 `standard`，保留任务子表、执行历史及归档属性。从 3.0.0 升级时还会按旧版 `CONFIRMED` 等同已归档的语义，仅为已有 `CONFIRMED` 任务回填原完成或更新时间并写入迁移历史；其他状态保持未归档。迁移结束后执行外键与完整性检查，再更新为 Schema 3.2.0。

## 安全边界

scope 必须相对 `E:\code` 并匹配项目清单中最长的项目路径。绝对路径、`..`、`$CODEX_HOME`、`.reasonix`、`.env` 和未登记项目会被拒绝。Worker 还必须读取目标项目适用的 `AGENTS.md`、检查 Git 工作树、保留既有改动并只处理已领取 scope。

删除、发布、Git 提交、外部消息和凭据访问需要明确人工授权；授权应体现在当前任务内容或 Operator 的明确指令中。缺少授权时以 `WAITING_HUMAN` 完成本轮。

## 服务健康

Dashboard Server 默认绑定 `127.0.0.1:4178`：

- `/`：监控页。
- `/api/state`：合并任务库、初始化配置、项目清单和运行时健康状态。
- `/healthz`：Schema、任务数和活动 execution 健康信息。

Windows 任务计划程序默认每 30 分钟直接运行一次 `health_run.py`，不调用 Codex 模型。服务正常时写入 `HEALTHY`；不可用时尝试恢复；连续达到阈值后写入 `NEEDS_ATTENTION`，但仍继续尝试自愈。Windows 恢复流程会核对端口监听 PID 的命令行，只停止本项目 Dashboard，并禁止多个 Dashboard 共享监听端口。健康状态保存在 `runtime/health-state.json`，独立短时锁避免健康任务重入。
