# Worker Reports

每个并发 Worker 在这里写入本轮唯一任务的结构化结果报告，再由 `scripts/loopctl.py finish` 校验并原子写入 SQLite。

报告支持三种状态：

- `SUCCEEDED`：必须包含非空 `summary` 和 `verification`。
- `FAILED`：必须包含非空 `summary` 和 `error`。
- `WAITING_HUMAN`：必须包含非空 `summary` 和 `question`，可包含 `options`。

报告文件使用 UTF-8 JSON，文件名格式为 `<execution-id>-<task-id>.json`。
