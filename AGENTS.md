# Local Agent Loop 操作约束

1. 所有文本文件按 UTF-8 读写，时间统一为带时区的 Asia/Shanghai ISO 8601。
2. `data/loop-agent.sqlite3` 只保存任务及其执行一致性数据，是任务的唯一事实来源。禁止重建运行时 `TASKS.json` 或 `INBOX.json`。
3. 自动化周期、任务执行参数和服务部署参数只读取 `config/initialization.json`，不得保存到 SQLite。
4. 项目路由实时读取 `E:\code\根目录清单.md`，不得缓存到 SQLite。
5. 健康状态只写入 `runtime/health-state.json`，不得保存到 SQLite。
6. 人工主对话是 Operator，必须遵循 `prompts/operator.md`；只管理任务，不执行任务业务内容。
7. Worker 自动化的权威提示词是 `prompts/worker.md`；每次只调用一次 `claim`，只处理领取结果中的一个任务，不创建或等待其他任务。
8. Worker 用 stdin 把结构化结果交给 `finish`；正常流程不得创建持久化 reports。
9. 健康检查由 Windows 任务计划程序直接运行 `health_run.py`，不得通过 Codex 自动化运行，也不领取任务。
10. 不物理删除任务；使用 `cancel` 保留审计历史。
11. 不直接写 SQLite 表；任务状态变更必须通过 `scripts/loopctl.py`。
12. 不读取或输出 `.env`、凭据、密钥、`$CODEX_HOME` 和 `.reasonix`。
13. 保留用户已有改动，遵守目标项目的 `AGENTS.md`，只修改任务 scope。
14. 自动执行不能生成 `CONFIRMED`；它只能由人工复核后的 `confirm` 命令产生。
