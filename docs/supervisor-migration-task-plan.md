# Supervisor 迁移任务计划

## 目标

在不影响当前 Loop Agent 运行链路的前提下，逐步形成以下架构：

```text
Codex 客户端 Operator
        |
        v
自建 Supervisor
        |
        +-- 调度、租约、heartbeat、重试、健康检查
        +-- Codex CLI Runner
        +-- Dashboard / API
        |
        v
SQLite 任务库
```

Operator 继续使用 Codex 客户端负责任务管理、人工确认和重新排队；Supervisor 负责调度和执行进程管理；Codex CLI 负责具体业务执行。

## 不变边界

- 当前 Codex 客户端自动化继续运行，不提前停止或替换。
- 现有 SQLite 任务模型、`claim`、`heartbeat`、`finish`、优先级、依赖和 scope 锁优先复用。
- 新 Supervisor 在灰度前不得对当前生产队列执行 `claim`。
- 开发和验证优先使用独立配置、独立测试数据库和独立日志目录。
- 生产切换必须保留人工确认和回滚路径。
- 当前 Windows 健康任务在迁移期间继续作为外部兜底。

## 任务清单

| 步骤 | 任务 ID | 优先级 | 依赖 | 任务目标 |
|---|---|---:|---|---|
| 1 | `LOOP-SUPERVISOR-BASELINE-001` | critical | 无 | 记录当前 Worker、健康检查、Dashboard、SQLite 和自动化运行基线，形成不影响现网的约束清单。 |
| 2 | `LOOP-SUPERVISOR-COMPAT-CONTRACT-001` | critical | 1 | 明确旧 Codex 自动化与新 Supervisor 共存期间的兼容边界、状态机、租约和切换规则。 |
| 3 | `LOOP-SUPERVISOR-RUNNER-CONTRACT-001` | high | 2 | 定义统一 Runner 接口和事件协议，抽象 Codex CLI、OpenAI API 和其他 Agent 的共同能力。 |
| 4 | `LOOP-SUPERVISOR-ISOLATED-HARNESS-001` | high | 3 | 建立独立测试配置、测试数据库、日志目录和模拟任务，确保开发验证不接触生产队列。 |
| 5 | `LOOP-SUPERVISOR-CLI-RUNNER-001` | high | 3、4 | 实现单任务单 `codex exec` 的启动、输入、JSONL 读取、最终结果和退出码处理。 |
| 6 | `LOOP-SUPERVISOR-PROCESS-GUARD-001` | critical | 5 | 管理 CLI PID、子进程树、超时、崩溃、取消和孤儿进程回收。 |
| 7 | `LOOP-SUPERVISOR-HEARTBEAT-001` | critical | 5、6 | 将 CLI 进程生命周期映射到现有 `claim / heartbeat / finish` 协议。 |
| 8 | `LOOP-SUPERVISOR-SCHEDULER-001` | critical | 2、7 | 实现 Supervisor 调度循环，处理优先级、并发上限、任务领取、重试和人工介入。 |
| 9 | `LOOP-SUPERVISOR-HEALTH-001` | medium | 6、8 | 统一检查 Supervisor、CLI Worker、SQLite 和 Dashboard，同时保留现有外部健康任务作为兜底。 |
| 10 | `LOOP-SUPERVISOR-OBSERVABILITY-001` | medium | 7、8、9 | 让 Dashboard 展示 Supervisor、CLI 进程、最后事件、退出原因和执行健康状态。 |
| 11 | `LOOP-SUPERVISOR-INTEGRATION-TEST-001` | critical | 4、5、6、7、8、9 | 覆盖正常完成、`WAITING_HUMAN`、超时、CLI 崩溃、重启、scope 冲突、并发上限和服务恢复。 |
| 12 | `LOOP-SUPERVISOR-SHADOW-001` | high | 10、11 | 以只读或独立测试队列运行 Supervisor，不允许领取当前生产任务，验证长期稳定性。 |
| 13 | `LOOP-SUPERVISOR-CANARY-001` | high | 12 | 在明确路由和人工批准下，让少量指定任务使用 Codex CLI，旧 Worker 继续处理其他任务。 |
| 14 | `LOOP-SUPERVISOR-SERVICE-DEPLOY-001` | high | 11、12 | 将 Supervisor 注册为 Windows 服务，实现开机启动、自动重启、日志和服务账户配置。 |
| 15 | `LOOP-SUPERVISOR-CUTOVER-001` | critical | 13、14 | 经人工确认后切换生产调度，保留回滚路径，确认旧 Codex 自动化不再与新 Supervisor 竞争队列。 |
| 16 | `LOOP-AGENT-API-RUNNER-001` | low | 3、11 | 后续增加直接调用 OpenAI API 或其他 Agent 的 Runner；属于扩展项，不阻塞 Codex CLI 迁移。 |

## 可复用内容

以下内容原则上不重写：

- SQLite 任务模型
- `claim / heartbeat / finish`
- 优先级、依赖和 scope 锁
- 当前 Operator 提示词和任务管理流程
- Dashboard 基础页面
- 当前 Windows 健康检查（迁移期间作为兜底）

## 阶段门禁

### 开发阶段

任务 1 至 11 只允许在独立测试环境完成，不得让新 Supervisor 领取当前生产任务。

### 影子阶段

任务 12 只能观测或使用独立测试队列，不得改变现有任务状态、执行结果或 scope 锁。

### 灰度阶段

任务 13 必须具备明确的任务路由和人工批准。当前 `claim` 逻辑不会自动按 Runner 类型隔离队列，因此不能仅通过增加一个 `runner` 字段实现生产隔离。

### 切换阶段

任务 15 必须人工确认。切换前应停止旧调度入口或明确其不再领取生产任务，并验证 Supervisor 的回滚路径。

## 当前状态

本文档只是迁移任务计划：

- 尚未将这些任务写入 Loop SQLite。
- 尚未修改当前运行中的 Codex 自动化。
- 尚未实现 Supervisor、CLI Runner 或 Windows 服务。
