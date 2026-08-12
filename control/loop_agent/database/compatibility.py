"""探测仍可能使用旧任务 Schema 的数据库能力。

CLI 必须能够检查和迁移旧数据库，因此迁移完成前不能假设所有新列都存在。这里集中保存
Schema 迁移、数据库校验和只读状态投影使用的少量 PRAGMA 检查。
"""

from __future__ import annotations

# 中文排查：这些函数只探测旧数据库是否具备某列或某代 Schema 能力。
# 迁移分支判断错误时先打印表结构而不是任务内容，再核对对应 capability 函数。
# 兼容探测必须只读，不能在检测过程中偷偷执行 DDL 或修补数据。

import sqlite3


LEGACY_PROFILE_TO_CAPABILITY = {
    "routine": "L1",
    "standard": "L2",
    "advanced": "L3",
    "deep": "L4",
    "complex": "L5",
    "exceptional": "L5",
}


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
