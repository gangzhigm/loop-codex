# Local Agent Loop 操作约束

1. 所有文本文件按 UTF-8 读写，时间统一为带时区的 Asia/Shanghai ISO 8601。
2. `loop-agent.sqlite3` 是任务及其执行一致性数据的唯一事实来源。禁止重建运行时 `TASKS.json` 或 `INBOX.json`。
3. 自动化周期和服务部署参数只读取 `config/initialization.json`，不得写入或从 SQLite `settings` 读取。
4. 人工主对话是 Operator，只添加、修改、取消、重排和确认任务，不执行任务业务内容。
5. Worker 自动化每次只调用一次 `claim`，只处理领取结果中的一个任务，不创建或等待其他对话。
6. Health 自动化只运行 `health_run.py`，不领取任务。
7. 不物理删除任务；使用 `cancel` 保留审计历史。
8. 不直接写 SQLite 表；任务状态变更必须通过 `scripts/loopctl.py`。
9. 不读取或输出 `.env`、凭据、密钥、`$CODEX_HOME` 和 `.reasonix`。
10. 保留用户已有改动，遵守目标项目的 `AGENTS.md`，只修改任务 scope。
11. 自动执行不能生成 `CONFIRMED`；它只能由人工复核后的 `confirm` 命令产生。
