# Prompt Sources

此目录是 Local Agent Loop 提示词的唯一维护位置：

- `operator.md`：人工任务管理主对话的查重、拆分、依赖、优先级、执行档位、状态、独立归档和 Worker 启停边界。
- `worker.md`：Codex Worker 自动化的档位领取与完整执行提示词。
- `cli-worker.md`：Codex CLI Runner 已领取单任务后的业务执行与结构化结果提示词。

`operator.md` 由任务管理对话遵循；Codex Worker 自动化的入口提示只负责读取并执行 `worker.md`，不复制正文。Windows 健康任务直接运行确定性脚本，不使用模型提示词。

五条普通 Worker 自动化共享 `worker.md`，入口提示固定提供 `runtime_environment=codex_automation` 和当前 `execution_profile`，不得复制正文。Codex CLI Runner 使用 `cli-worker.md`，由 Runner 负责一次 claim、heartbeat 和 finish，CLI 子进程不得管理队列。DeepSeek 使用自建 Agent Runtime 的中立上下文。各入口调用 `claim` 时必须显式声明 `codex_automation`、`codex_cli` 或 `deepseek`；`exceptional` 使用人工批准的一次性执行。修改提示词后下一次对应 Worker 直接读取新内容；修改环境、档位、结构或路径时必须同步更新初始化配置、文档和真实执行入口。
