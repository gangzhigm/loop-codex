# Local Agent Loop Planner

Planner 是单实例常驻调度服务，包含彼此独立的“预检排队”和“正式执行分发”两条链路。
它只安排工作，不领取任务，也不直接调用模型。
周期、容量、运行环境和 Provider 配置统一读取 `config/initialization.json`。

草稿预检状态严格表达实际介入阶段：

- `DRAFT/UNINSPECTED`：Planner 尚未介入。
- `DRAFT/QUEUED`：Planner 已完成排队，AI 尚未介入。
- `DRAFT/INSPECTING`：预检 Runner 已领取同一条排队 execution，AI 已介入。

禁止从 `UNINSPECTED` 直接进入 `INSPECTING`。Planner Scheduler 只执行第一段状态转换；
后续 Runner 必须使用 Planner 创建的 execution-id 领取 `QUEUED` 任务。

运行期由 `planner/main.py` 统一维护 PID、heartbeat、停止请求和信号处理。服务启动后立即
运行所有已启用的调度链，后续各自按配置周期独立重复。

预检排队周期由 `planner.scheduler.interval_minutes` 定义。每轮用
`planner.max_active_executions` 减去 `DRAFT/QUEUED` 与 `DRAFT/INSPECTING` 数量得到空闲
槽位，并按公共 `priority_policy.levels`、创建时间和任务 ID 选择 `DRAFT/UNINSPECTED`。

Scheduler 通过 `control/loopctl.py schedule-preflight` 在一个事务中为每个选中任务生成
独立 execution-id，将任务改为 `DRAFT/QUEUED`，并创建 `PLANNER/QUEUED` execution。
数据库是 Planner 与后续 Runner 的持久交付边界。

正式执行分发周期由 `planner.execution_scheduler.interval_minutes` 定义。每轮只读选择首个
依赖已完成、运行环境与 Provider 匹配的 `PENDING/READY` 自动任务，并在全局及平台容量
允许时最多启动一个 `runner/agent_runtime.py`。Runner 随后通过 `control/loopctl.py claim`
原子领取任务；若领取失败，任务状态保持事实一致。

两条链路的状态机、周期、容量限制和故障结果互不混用。关闭其中一条不会关闭另一条；只有
两条链路都关闭时 Planner 才不应运行。已经排队的预检任务会占用 Planner 预检并发槽位，
后续周期不会重复排队。独立 Dispatcher 服务已取消，其执行分发能力归 Planner 所有。
