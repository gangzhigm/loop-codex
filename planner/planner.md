# Local Agent Loop Planner

Planner 当前实现到“选择任务并交付给 Runner”的阶段，尚未启用 AI 静态预检。

运行期由 `planner/main.py` 维护 PID、heartbeat、停止请求和信号处理。服务启动后立即从
权威数据库读取一次 `status=DRAFT` 的完整任务投影，之后按
`config/initialization.json` 中 `planner.scheduler.interval_minutes` 的周期重复执行。每轮用
`planner.max_active_executions` 减去 `DRAFT/INSPECTING` 数量得到空闲槽位，并按公共
`priority_policy.levels`、创建时间和任务 ID 选择 `DRAFT/UNINSPECTED`。

Scheduler 为每个选中任务生成独立 execution-id，启动 `runner/planner_runner.py`，并显式
传入 task-id、数据库和配置路径。阶段版 Runner 只读核对任务并向 Runner 日志输出
`PLANNER_TASK_RECEIVED`；它不加载 Provider、不调用模型、不领取预检 execution，也不写
SQLite。`planner/control.py` 已提供后续阶段需要的原子预检协议，但 Scheduler 和阶段版
Runner 当前都不调用这些命令。

由于本阶段没有把任务改成 `INSPECTING`，同一 `DRAFT/UNINSPECTED` 任务可能在下一周期
再次交付。这只能证明 Scheduler 到 Runner 的参数和进程边界可用，不能解释为预检已执行，
也不会自动形成 `PENDING/READY` 执行契约。
