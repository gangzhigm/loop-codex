# Local Agent Loop Planner

Planner 当前实现到“持久化预检队列”的阶段，尚未启用 AI 静态预检。

草稿预检状态严格表达实际介入阶段：

- `DRAFT/UNINSPECTED`：Planner 尚未介入。
- `DRAFT/QUEUED`：Planner 已完成排队，AI 尚未介入。
- `DRAFT/INSPECTING`：预检 Runner 已领取同一条排队 execution，AI 已介入。

禁止从 `UNINSPECTED` 直接进入 `INSPECTING`。Planner Scheduler 只执行第一段状态转换；
后续 Runner 必须使用 Planner 创建的 execution-id 领取 `QUEUED` 任务。

运行期由 `planner/main.py` 维护 PID、heartbeat、停止请求和信号处理。服务启动后立即从
权威数据库执行一次预检排队，之后按
`config/initialization.json` 中 `planner.scheduler.interval_minutes` 的周期重复执行。每轮用
`planner.max_active_executions` 减去 `DRAFT/QUEUED` 与 `DRAFT/INSPECTING` 数量得到空闲
槽位，并按公共 `priority_policy.levels`、创建时间和任务 ID 选择 `DRAFT/UNINSPECTED`。

Scheduler 通过 `control/loopctl.py schedule-preflight` 在一个事务中为每个选中任务生成
独立 execution-id，将任务改为 `DRAFT/QUEUED`，并创建 `PLANNER/QUEUED` execution。
数据库是 Planner 与后续 Runner 的持久交付边界。

本阶段不启动 Runner、不直接通信、不加载 Provider、不调用模型，也不会自动形成
`PENDING/READY` 执行契约。已经排队的任务会占用 Planner 并发槽位，后续周期不会重复排队。
