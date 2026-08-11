"""共享的队列兼容处理和依赖就绪检查。"""

from __future__ import annotations

# 中文排查：这里只计算依赖就绪和旧 WAITING_CONFLICT 数据的兼容回队，不负责真正 claim。
# 任务看似可执行却未入队时，先逐个检查依赖终态，再核对是否仍有旧冲突记录。
# 新冲突应动态投影并保持 PENDING，不要重新把 WAITING_CONFLICT 当作主要状态。

import sqlite3
from typing import Any

from loop_agent.constants import DEPENDENCY_COMPLETE_STATUSES
from loop_agent.serialization import now_shanghai


def requeue_resolved_conflicts(database: sqlite3.Connection) -> list[str]:
    rows = database.execute("SELECT id FROM tasks WHERE status='WAITING_CONFLICT'").fetchall()
    stamp = now_shanghai()
    requeued: list[str] = []
    for row in rows:
        database.execute(
            "UPDATE tasks SET status='PENDING', updated_at=?, "
            "progress_summary='旧 WAITING_CONFLICT 已转换为动态 scope 排队。', "
            "progress_next_step='等待 scope 可用后由下一次 claim 自动领取。', row_version=row_version+1 WHERE id=?",
            (stamp, row["id"]),
        )
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, 'WAITING_CONFLICT', 'PENDING', 'conflict-compatibility', "
            "'旧冲突状态已恢复为 PENDING；原 blocker 记录保留用于审计，当前阻塞改为动态投影。')",
            (row["id"], stamp),
        )
        requeued.append(row["id"])
    return requeued


def dependencies_ready(database: sqlite3.Connection, task_id: str) -> bool:
    rows = database.execute(
        "SELECT dependency_id FROM task_dependencies WHERE task_id=?", (task_id,)
    ).fetchall()
    for row in rows:
        dependency = database.execute("SELECT status FROM tasks WHERE id=?", (row[0],)).fetchone()
        if not dependency or dependency[0] not in DEPENDENCY_COMPLETE_STATUSES:
            return False
    return True


