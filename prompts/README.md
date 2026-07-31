# Prompt Sources

此目录是 Local Agent Loop 提示词的唯一维护位置：

- `operator.md`：人工任务管理主对话的查重、拆分、依赖、优先级、执行档位、状态、独立归档和 Worker 启停边界。
- `worker.md`：Codex Worker 自动化的档位领取与完整执行提示词。

`operator.md` 由任务管理对话遵循；Codex Worker 自动化的入口提示只负责读取并执行 `worker.md`，不复制正文。Windows 健康任务直接运行确定性脚本，不使用模型提示词。

五条普通 Worker 自动化共享 `worker.md`，入口提示只提供 `execution_profile`，不得复制正文。`exceptional` 使用人工批准的一次性 Codex 执行。修改 `worker.md` 后下一次 Worker 运行直接读取新内容；修改档位、结构或路径时必须同步更新初始化配置、文档和真实自动化入口。
