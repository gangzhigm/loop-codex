"""Feature probes for databases that may still use an older task schema.

The CLI must inspect and migrate older databases, so behavior cannot assume all
new columns exist before a migration completes. These probes centralize the
small PRAGMA checks used by task projection, validation, claim, and recovery.
"""

from __future__ import annotations

# 中文排查：这些函数只探测旧数据库是否具备某列或某代 Schema 能力。
# 迁移分支判断错误时先打印表结构而不是任务内容，再核对对应 capability 函数。
# 兼容探测必须只读，不能在检测过程中偷偷执行 DDL 或修补数据。

import sqlite3


def uses_capability_schema(database: sqlite3.Connection) -> bool:
    columns = {
        row[1] for row in database.execute("PRAGMA table_info(tasks)").fetchall()
    }
    return "capability_level" in columns


def uses_recovery_schema(database: sqlite3.Connection) -> bool:
    execution_columns = {
        row[1]
        for row in database.execute("PRAGMA table_info(executions)").fetchall()
    }
    lock_columns = {
        row[1]
        for row in database.execute("PRAGMA table_info(scope_locks)").fetchall()
    }
    return "recovery_required" in execution_columns and "status" in lock_columns


def uses_result_diagnostic_schema(database: sqlite3.Connection) -> bool:
    columns = {
        row[1] for row in database.execute("PRAGMA table_info(tasks)").fetchall()
    }
    return "result_diagnostic_json" in columns


def uses_preflight_schema(database: sqlite3.Connection) -> bool:
    columns = {
        row[1] for row in database.execute("PRAGMA table_info(tasks)").fetchall()
    }
    return "preflight_status" in columns
