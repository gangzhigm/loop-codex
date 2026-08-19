"""只读 Dashboard 状态投影和 revision 计算。

revision 完全由任务和 execution 记录派生，不使用可变元数据表。``state_payload`` 在适配
旧路由 Schema 的同时，返回 Dashboard 当前要求的公开数据形状。
"""

from __future__ import annotations

# 中文排查：这里生成 Dashboard 使用的全量状态、统计值和派生 revision。
# 数量不一致时先检查 all_tasks 投影，再检查状态集合、归档条件和统计去重逻辑。
# 该模块只读数据库；任何为了修正展示而写任务状态的做法都属于越界。

import sqlite3
from typing import Any

from loop_agent.configuration import load_initialization_config
from loop_agent.database.compatibility import (
    LEGACY_PROFILE_TO_CAPABILITY,
    uses_capability_schema,
    uses_preflight_schema,
    uses_recovery_schema,
)
from loop_agent.database.schema import schema_version
from loop_agent.database.task_store import all_tasks
from loop_agent.serialization import now_shanghai
from loop_agent.tasks.scopes import configured_projects


def current_revision(database: sqlite3.Connection) -> int:
    """根据任务数据计算确定且足够单调的界面 revision。"""
    task_versions = int(
        database.execute(
            "SELECT COALESCE(sum(row_version), 0) FROM tasks"
        ).fetchone()[0]
    )
    histories = int(
        database.execute("SELECT count(*) FROM task_history").fetchone()[0]
    )
    executions = int(
        database.execute("SELECT count(*) FROM executions").fetchone()[0]
    )
    preflight_executions = (
        int(
            database.execute(
                "SELECT count(*) FROM preflight_executions"
            ).fetchone()[0]
        )
        if uses_preflight_schema(database)
        else 0
    )
    return task_versions + histories + executions + preflight_executions


def state_payload(
    database: sqlite3.Connection,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构建 Dashboard 使用的完整只读数据。"""
    value = config or load_initialization_config()
    if uses_capability_schema(database):
        executions = database.execute(
            "SELECT execution_id, task_id, heartbeat_at, runtime_environment, provider_id, capability_level, "
            "execution_policy, model, reasoning, attempt_timeout_seconds, max_retries FROM executions "
            "WHERE status='RUNNING' ORDER BY started_at"
        ).fetchall()
        agents = [
            {
                "id": row["execution_id"],
                "role": "worker",
                "execution_kind": "WORKER",
                "status": "RUNNING",
                "current_task_id": row["task_id"],
                "last_seen_at": row["heartbeat_at"],
                "capability_level": row["capability_level"],
                "execution_policy": row["execution_policy"],
                "runtime_environment": row["runtime_environment"],
                "provider_id": row["provider_id"],
                "execution_config": {
                    "model": row["model"],
                    "reasoning": row["reasoning"],
                    "attempt_timeout_seconds": row["attempt_timeout_seconds"],
                    "max_retries": row["max_retries"],
                },
                "summary": (
                    f"Concurrent SQLite worker · {row['runtime_environment']} · "
                    f"{row['capability_level']}"
                ),
            }
            for row in executions
        ]
    else:
        executions = database.execute(
            "SELECT e.execution_id, e.task_id, e.heartbeat_at, t.execution_profile, t.runtime_environment "
            "FROM executions e JOIN tasks t ON t.id=e.task_id "
            "WHERE e.status='RUNNING' ORDER BY e.started_at"
        ).fetchall()
        agents = []
        for row in executions:
            runtime_environment = (
                "self_hosted_agent"
                if row["runtime_environment"] == "deepseek"
                else row["runtime_environment"]
            )
            agents.append(
                {
                    "id": row["execution_id"],
                    "role": "worker",
                    "execution_kind": "WORKER",
                    "status": "RUNNING",
                    "current_task_id": row["task_id"],
                    "last_seen_at": row["heartbeat_at"],
                    "capability_level": LEGACY_PROFILE_TO_CAPABILITY[
                        row["execution_profile"]
                    ],
                    "execution_policy": (
                        "manual"
                        if row["execution_profile"] == "exceptional"
                        else "automatic"
                    ),
                    "runtime_environment": runtime_environment,
                    "provider_id": (
                        "deepseek"
                        if row["runtime_environment"] == "deepseek"
                        else None
                    ),
                    "summary": (
                        f"Concurrent SQLite worker · {runtime_environment} · "
                        f"{row['execution_profile']}"
                    ),
                }
            )
    planners: list[dict[str, Any]] = []
    if uses_preflight_schema(database):
        planners = [
            dict(row)
            for row in database.execute(
                "SELECT execution_id AS id, task_id AS current_task_id, execution_kind, status, "
                "started_at, heartbeat_at AS last_seen_at, lease_expires_at, attempt_deadline_at "
                "FROM preflight_executions WHERE status IN ('QUEUED', 'INSPECTING') ORDER BY started_at"
            ).fetchall()
        ]
    recoveries: list[dict[str, Any]] = []
    if uses_recovery_schema(database):
        rows = database.execute(
            "SELECT e.execution_id, e.task_id, e.status, e.finished_at, e.termination_reason, "
            "e.runtime_environment, e.provider_id, e.capability_level, e.execution_policy, "
            "e.recovered_at, e.recovery_action, l.scope_key, l.status AS scope_status, "
            "l.quarantined_at, l.quarantine_reason FROM executions e "
            "LEFT JOIN scope_locks l ON l.execution_id=e.execution_id "
            "WHERE e.recovery_required=1 ORDER BY e.started_at, l.scope_key"
        ).fetchall()
        by_execution: dict[str, dict[str, Any]] = {}
        for row in rows:
            recovery = by_execution.setdefault(
                row["execution_id"],
                {
                    "execution_id": row["execution_id"],
                    "task_id": row["task_id"],
                    "execution_status": row["status"],
                    "termination_reason": row["termination_reason"],
                    "runtime_environment": row["runtime_environment"],
                    "provider_id": row["provider_id"],
                    "capability_level": row["capability_level"],
                    "execution_policy": row["execution_policy"],
                    "finished_at": row["finished_at"],
                    "recovered_at": row["recovered_at"],
                    "recovery_action": row["recovery_action"],
                    "scope_status": row["scope_status"],
                    "quarantined_at": row["quarantined_at"],
                    "quarantine_reason": row["quarantine_reason"],
                    "scope_keys": [],
                },
            )
            if row["scope_key"] is not None:
                recovery["scope_keys"].append(row["scope_key"])
        recoveries = list(by_execution.values())
    updated = (
        database.execute("SELECT max(updated_at) FROM tasks").fetchone()[0]
        or now_shanghai()
    )
    workspace = value["workspace"]
    return {
        "schema_version": schema_version(database),
        "workspace": {
            "name": workspace["name"],
            "timezone": workspace["timezone"],
            "revision": current_revision(database),
            "updated_at": updated,
            "writer": "sqlite-task-store",
            "task_root": workspace["task_root"],
            "project_registry": workspace["project_registry"],
        },
        "settings": value["task_execution"],
        "agents": agents,
        "planners": planners,
        "planner_settings": value.get("planner", {}),
        "recoveries": recoveries,
        "tasks": all_tasks(database),
        "services": [],
        "health_events": [],
        "projects": configured_projects(value),
    }


def bump_revision(database: sqlite3.Connection, writer: str) -> int:
    """兼容接口：revision 为派生值，因此 writer 不影响审计结果。"""
    del writer
    return current_revision(database)
