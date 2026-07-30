# Prompt Sources

此目录是 Local Agent Loop 提示词的唯一维护位置：

- `operator.md`：人工任务管理主对话的操作边界和流程。
- `worker.md`：Codex Worker 自动化的完整执行提示词。

`operator.md` 由任务管理对话遵循；Codex Worker 自动化的入口提示只负责读取并执行 `worker.md`，不复制正文。Windows 健康任务直接运行确定性脚本，不使用模型提示词。

修改 `worker.md` 后下一次 Worker 运行直接读取新内容。修改提示词结构或路径时才需要同步更新配置、文档和真实自动化入口。
