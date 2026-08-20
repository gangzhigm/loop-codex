# Local Agent Loop Planner

Planner 是任务形成执行契约之前的预检业务阶段，不是常驻调度服务。它定义草稿任务的
排队、领取、heartbeat 和结果写回状态机；周期触发由 `scheduler/main.py` 负责。
预检容量、运行环境、Provider 和安全边界统一读取 `config/initialization.json`。

草稿预检状态严格表达实际介入阶段：

- `DRAFT/UNINSPECTED`：Planner 尚未介入。
- `DRAFT/QUEUED`：Planner 已完成排队，AI 尚未介入。
- `DRAFT/INSPECTING`：预检 Runner 已领取同一条排队 execution，AI 已介入。

禁止从 `UNINSPECTED` 直接进入 `INSPECTING`。Scheduler 只执行第一段状态转换；
后续 Runner 必须使用 Planner 创建的 execution-id 领取 `QUEUED` 任务。

预检排队周期由 `scheduler.preflight.interval_minutes` 定义。每轮用
`planner.max_active_executions` 减去 `DRAFT/QUEUED` 与 `DRAFT/INSPECTING` 数量得到空闲
槽位，并按公共 `priority_policy.levels`、创建时间和任务 ID 选择 `DRAFT/UNINSPECTED`。

Scheduler 通过 `control/loopctl.py schedule-preflight` 在一个事务中为每个选中任务生成
独立 execution-id，将任务改为 `DRAFT/QUEUED`，并创建 `PLANNER/QUEUED` execution。
数据库是 Planner 与后续 Runner 的持久交付边界。

Planner 只拥有预检业务状态，不拥有正式 Worker 执行排队。Scheduler 的另一条独立链路
负责把 `PENDING/READY` 自动任务原子排入 `QUEUED/READY`，并创建 `WORKER/QUEUED`
execution。独立 Runner 读取 Planner 与 Worker 队列、计算容量并选择候选；当前不启动 AI
Worker。两条排队链不得混用状态或容量，已经排队的任务不会被 Scheduler 重复排队。
