# Local Agent Loop Planner

Planner 业务正在重新设计，当前只实现只读的 DRAFT 任务发现和选择，还没有可执行的预检协议。

运行期由 `planner/main.py` 维护 PID、heartbeat、停止请求和信号处理。服务启动后立即从
权威任务数据库读取一次 `status=DRAFT` 的完整任务投影，之后按
`config/initialization.json` 中 `planner.scheduler.interval_minutes` 的周期重复读取。每轮用
`planner.max_active_executions` 减去 `DRAFT/INSPECTING` 数量得到空闲槽位，并按公共
`priority_policy.levels`、创建时间和任务 ID 选择 `DRAFT/UNINSPECTED`。

当前查询和选择严格只读。Planner 不领取任务、不启动 Runner、不调用模型，不改变任务状态，
也不写 SQLite；选择结果只保留在当前轮内，Scheduler 日志记录槽位、数量和任务 ID 摘要。

`config/initialization.json` 暂时保留 Planner 的旧配置和数据契约，供后续开发与历史数据
兼容使用；这些配置不表示旧预检流程仍然可用。
