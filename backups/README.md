# Backups

`sqlite-migration-20260729T180540/` 是当前保留的唯一旧系统快照，包含：

- 迁移源 `TASKS.json` 和 `INBOX.json`。
- 迁移前的 Worker 自动化配置。

这些文件只用于迁移审计和灾难恢复，不是运行时数据源。正常运行不得从备份读取任务，也不得把备份复制回 Loop 根目录。
