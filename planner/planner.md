# Local Agent Loop Planner

Planner 业务正在重新设计，当前没有可执行的角色协议。

运行期只启用 `planner/main.py` 中的 PID、heartbeat、停止请求和信号处理。该服务不领取
任务、不启动 Runner、不调用模型，也不读写任务数据库。

`config/initialization.json` 暂时保留 Planner 的旧配置和数据契约，供后续开发与历史数据
兼容使用；这些配置不表示旧预检流程仍然可用。
