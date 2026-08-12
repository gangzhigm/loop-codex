"""SQLite Schema 原地迁移命令。

迁移只作用于现有任务数据库并保留其中的任务、执行、锁和审计数据；系统不再提供旧
JSON 文件或旧程序入口的导入能力。
"""

from __future__ import annotations

import argparse

from loop_agent.control.io import output
from loopdb import LoopError, connect, migrate_schema, validate_database


def command_migrate(args: argparse.Namespace) -> None:
    """升级现有 SQLite Schema，并在完成后验证数据库一致性。"""
    database = connect(args.db)
    try:
        result = migrate_schema(database)
        validation = validate_database(database)
        if not validation["ok"]:
            raise LoopError(f"迁移后校验失败: {validation}")
        output({"outcome": "MIGRATED" if result["migrated"] else "ALREADY_CURRENT", **result})
    finally:
        database.close()
