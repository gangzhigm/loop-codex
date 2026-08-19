"""SQLite 任务存储的跨表一致性校验。

校验过程刻意保持全面且只读。它会检查 SQLite 自身完整性、仅任务表允许列表、工作流和历史
不变量、执行路由、Planner 状态、恢复隔离、并发限制、依赖环及 scope 锁兼容性。返回的错误
列表同时适用于 CLI 诊断和初始化检查。
"""

from __future__ import annotations

# 中文排查：本模块执行跨表、状态、execution、scope 锁和结果诊断的一致性验证。
# validate 失败时优先处理返回 errors 的第一项，因为后续错误可能只是同一根因的连锁结果。
# 验证器必须保持只读且给出可定位信息，不能在检查时自动修复真实任务。

import sqlite3
from datetime import datetime
from typing import Any

from loop_agent.configuration import load_initialization_config
from loop_agent.constants import CANONICAL_RUNTIME_ENVIRONMENTS, SCHEMA_VERSION
from loop_agent.database.compatibility import (
    uses_capability_schema,
    uses_preflight_schema,
    uses_recovery_schema,
    uses_result_diagnostic_schema,
)
from loop_agent.database.execution_settings import (
    global_parallel_limit,
    platform_parallel_limit,
)
from loop_agent.database.schema import schema_version, uses_hybrid_scope_schema
from loop_agent.database.task_store import dependency_cycle_path
from loop_agent.errors import LoopError
from loop_agent.tasks.normalization import load_result_diagnostic
from loop_agent.tasks.scopes import parse_scope_key, scope_keys_conflict


ALLOWED_TABLES = {
    "tasks",
    "task_dependencies",
    "task_scopes",
    "task_acceptance",
    "task_technical_acceptance",
    "task_preflight_evidence",
    "task_completed_items",
    "task_verifications",
    "task_attachments",
    "task_history",
    "executions",
    "preflight_executions",
    "scope_locks",
    "task_conflicts",
}


def validate_database(
    database: sqlite3.Connection,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """返回结构化校验结果，不修改数据库。"""
    value = config or load_initialization_config()
    errors: list[str] = []
    version = schema_version(database)
    if version != SCHEMA_VERSION:
        errors.append(f"schema_version={version}, expected={SCHEMA_VERSION}")
    foreign_keys = database.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        errors.append(f"foreign_key_check={len(foreign_keys)}")
    integrity = database.execute("PRAGMA quick_check").fetchone()[0]
    if integrity != "ok":
        errors.append(f"quick_check={integrity}")
    actual_tables = {
        row[0]
        for row in database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    unexpected = sorted(actual_tables - ALLOWED_TABLES)
    missing = sorted(ALLOWED_TABLES - actual_tables)
    if unexpected:
        errors.append("数据库包含非任务表: " + ",".join(unexpected))
    if missing:
        errors.append("数据库缺少任务表: " + ",".join(missing))
    if uses_result_diagnostic_schema(database):
        invalid_diagnostics: list[str] = []
        for row in database.execute(
            "SELECT id, result_diagnostic_json FROM tasks WHERE result_diagnostic_json IS NOT NULL"
        ):
            try:
                load_result_diagnostic(row["result_diagnostic_json"])
            except LoopError:
                invalid_diagnostics.append(row["id"])
        if invalid_diagnostics:
            errors.append("任务结果诊断无效: " + ",".join(invalid_diagnostics))
    if uses_preflight_schema(database) and not uses_hybrid_scope_schema(database):
        errors.append("当前 Schema 3.8.0 尚未迁移到混合 scope 锁结构")
    invalid_confirmed = database.execute(
        """SELECT t.id FROM tasks t WHERE t.status='CONFIRMED' AND NOT EXISTS (
          SELECT 1 FROM task_history h WHERE h.task_id=t.id AND h.to_status='CONFIRMED' AND h.from_status='SUCCEEDED'
        )"""
    ).fetchall()
    if invalid_confirmed:
        errors.append(
            "CONFIRMED 缺少 SUCCEEDED 转入历史: "
            + ",".join(row[0] for row in invalid_confirmed)
        )
    invalid_archived: list[str] = []
    for row in database.execute(
        "SELECT id, archived_at FROM tasks WHERE archived_at IS NOT NULL"
    ):
        try:
            parsed = datetime.fromisoformat(row["archived_at"])
            if parsed.utcoffset() is None:
                raise ValueError("missing timezone")
        except (TypeError, ValueError):
            invalid_archived.append(row["id"])
    if invalid_archived:
        errors.append(
            "archived_at 不是带时区 ISO 8601: " + ",".join(invalid_archived)
        )
    invalid_priorities = database.execute(
        "SELECT id FROM tasks WHERE priority NOT IN ('blocker','critical','high','medium','low')"
    ).fetchall()
    if invalid_priorities:
        errors.append(
            "任务优先级无效: " + ",".join(row[0] for row in invalid_priorities)
        )
    capability_schema = uses_capability_schema(database)
    if capability_schema:
        invalid_capabilities = database.execute(
            "SELECT id FROM tasks WHERE capability_level NOT IN ('L1','L2','L3','L4','L5')"
        ).fetchall()
        if invalid_capabilities:
            errors.append(
                "任务能力等级无效: "
                + ",".join(row[0] for row in invalid_capabilities)
            )
        invalid_policies = database.execute(
            "SELECT id FROM tasks WHERE execution_policy NOT IN ('automatic','manual')"
        ).fetchall()
        if invalid_policies:
            errors.append(
                "任务执行策略无效: "
                + ",".join(row[0] for row in invalid_policies)
            )
        invalid_provider_routes = database.execute(
            "SELECT id FROM tasks WHERE (runtime_environment='self_hosted_agent' AND "
            "(provider_id IS NULL OR length(trim(provider_id))=0)) OR "
            "(runtime_environment<>'self_hosted_agent' AND provider_id IS NOT NULL)"
        ).fetchall()
        if invalid_provider_routes:
            errors.append(
                "任务 Provider 路由无效: "
                + ",".join(row[0] for row in invalid_provider_routes)
            )
        invalid_snapshots = database.execute(
            "SELECT execution_id FROM executions WHERE model='' OR attempt_timeout_seconds<=0 OR max_retries<0"
        ).fetchall()
        if invalid_snapshots:
            errors.append(
                "execution 配置快照无效: "
                + ",".join(row[0] for row in invalid_snapshots)
            )
        if uses_preflight_schema(database):
            invalid_execution_kinds = database.execute(
                "SELECT execution_id FROM executions WHERE execution_kind<>'WORKER'"
            ).fetchall()
            if invalid_execution_kinds:
                errors.append(
                    "Worker execution_kind 无效: "
                    + ",".join(row[0] for row in invalid_execution_kinds)
                )
            invalid_preflight_states = database.execute(
                """SELECT id FROM tasks WHERE
                  (status='DRAFT' AND preflight_status NOT IN ('UNINSPECTED','QUEUED','INSPECTING')) OR
                  (status='NEEDS_REVIEW' AND preflight_status<>'FAILED') OR
                  (status IN ('PENDING','RUNNING','WAITING_CONFLICT','WAITING_HUMAN') AND preflight_status<>'READY') OR
                  (preflight_status='READY' AND (capability_level IS NULL OR lock_mode IS NULL OR
                    NOT EXISTS (SELECT 1 FROM task_scopes s WHERE s.task_id=tasks.id) OR
                    NOT EXISTS (SELECT 1 FROM task_technical_acceptance a WHERE a.task_id=tasks.id) OR
                    NOT EXISTS (SELECT 1 FROM task_preflight_evidence e WHERE e.task_id=tasks.id))) OR
                  (status IN ('DRAFT','NEEDS_REVIEW') AND (capability_level IS NOT NULL OR lock_mode IS NOT NULL)) OR
                  (preflight_status IN ('QUEUED','INSPECTING') AND (preflight_execution_id IS NULL OR NOT EXISTS (
                    SELECT 1 FROM preflight_executions p WHERE p.execution_id=tasks.preflight_execution_id
                    AND p.task_id=tasks.id AND p.status=tasks.preflight_status))) OR
                  (preflight_status NOT IN ('QUEUED','INSPECTING') AND preflight_execution_id IS NOT NULL)"""
            ).fetchall()
            if invalid_preflight_states:
                errors.append(
                    "任务预检状态无效: "
                    + ",".join(row[0] for row in invalid_preflight_states)
                )
            invalid_planners = database.execute(
                """SELECT p.execution_id FROM preflight_executions p LEFT JOIN tasks t ON t.id=p.task_id
                WHERE p.execution_kind<>'PLANNER' OR
                  (p.status IN ('QUEUED','INSPECTING') AND (t.id IS NULL OR t.status<>'DRAFT' OR
                    t.preflight_status<>p.status OR t.preflight_execution_id<>p.execution_id)) OR
                  (p.status NOT IN ('QUEUED','INSPECTING') AND p.finished_at IS NULL)"""
            ).fetchall()
            if invalid_planners:
                errors.append(
                    "Planner execution 状态无效: "
                    + ",".join(row[0] for row in invalid_planners)
                )
        if uses_recovery_schema(database):
            invalid_recoveries = database.execute(
                "SELECT execution_id FROM executions WHERE "
                "(status IN ('STALLED','TIMED_OUT') AND (recovery_required<>1 OR finished_at IS NULL "
                "OR termination_reason IS NULL)) OR "
                "(status IN ('RUNNING','FINISHED','EXPIRED') AND recovery_required=1)"
            ).fetchall()
            if invalid_recoveries:
                errors.append(
                    "execution 恢复状态无效: "
                    + ",".join(row[0] for row in invalid_recoveries)
                )
    else:
        invalid_profiles = database.execute(
            "SELECT id FROM tasks WHERE execution_profile NOT IN "
            "('routine','standard','advanced','deep','complex','exceptional')"
        ).fetchall()
        if invalid_profiles:
            errors.append(
                "任务执行档位无效: "
                + ",".join(row[0] for row in invalid_profiles)
            )
    runtime_values = (
        "('codex_automation','codex_cli','self_hosted_agent')"
        if capability_schema
        else "('codex_automation','codex_cli','deepseek')"
    )
    invalid_runtime_environments = database.execute(
        f"SELECT id FROM tasks WHERE runtime_environment NOT IN {runtime_values}"
    ).fetchall()
    if invalid_runtime_environments:
        errors.append(
            "任务运行环境无效: "
            + ",".join(row[0] for row in invalid_runtime_environments)
        )
    dependency_cycle = dependency_cycle_path(database)
    if dependency_cycle:
        errors.append("循环依赖: " + " -> ".join(dependency_cycle))
    active = database.execute(
        "SELECT count(*) FROM executions WHERE status='RUNNING'"
    ).fetchone()[0]
    maximum = global_parallel_limit(value)
    if active > maximum:
        errors.append(
            f"active_executions={active} exceeds global maximum={maximum}"
        )
    for platform in CANONICAL_RUNTIME_ENVIRONMENTS:
        if capability_schema:
            platform_active = database.execute(
                "SELECT count(*) FROM executions WHERE status='RUNNING' AND runtime_environment=?",
                (platform,),
            ).fetchone()[0]
        else:
            legacy_platform = (
                "deepseek" if platform == "self_hosted_agent" else platform
            )
            platform_active = database.execute(
                "SELECT count(*) FROM executions e JOIN tasks t ON t.id=e.task_id "
                "WHERE e.status='RUNNING' AND t.runtime_environment=?",
                (legacy_platform,),
            ).fetchone()[0]
        platform_maximum = platform_parallel_limit(platform, value)
        if platform_active > platform_maximum:
            errors.append(
                f"platform={platform} active_executions={platform_active} "
                f"exceeds maximum={platform_maximum}"
            )
    orphan_running = database.execute(
        """SELECT t.id FROM tasks t WHERE t.status='RUNNING' AND NOT EXISTS (
          SELECT 1 FROM executions e WHERE e.task_id=t.id AND e.status='RUNNING'
        )"""
    ).fetchall()
    if orphan_running:
        errors.append(
            "RUNNING 任务缺少活动 execution: "
            + ",".join(row[0] for row in orphan_running)
        )
    if uses_recovery_schema(database):
        mismatched_locks = database.execute(
            """SELECT l.scope_key FROM scope_locks l
            LEFT JOIN executions e ON e.execution_id=l.execution_id
            LEFT JOIN tasks t ON t.id=l.task_id
            WHERE e.execution_id IS NULL OR e.task_id<>l.task_id OR t.id IS NULL OR
              (l.status='ACTIVE' AND (e.status<>'RUNNING' OR t.status<>'RUNNING')) OR
              (l.status='QUARANTINED' AND (
                e.status NOT IN ('STALLED','TIMED_OUT') OR e.recovery_required<>1 OR
                t.status<>'WAITING_HUMAN' OR l.quarantined_at IS NULL OR l.quarantine_reason IS NULL
              ))"""
        ).fetchall()
    else:
        mismatched_locks = database.execute(
            """SELECT l.scope_key FROM scope_locks l LEFT JOIN executions e ON e.execution_id=l.execution_id
            WHERE e.execution_id IS NULL OR e.status<>'RUNNING' OR e.task_id<>l.task_id"""
        ).fetchall()
    if mismatched_locks:
        errors.append(
            "scope 锁与 execution 生命周期不一致: "
            + ",".join(row[0] for row in mismatched_locks)
        )
    invalid_scope_keys: list[str] = []
    for row in database.execute(
        "SELECT task_id, scope_key FROM task_scopes ORDER BY task_id, ordinal"
    ):
        try:
            parse_scope_key(row["scope_key"])
        except LoopError:
            invalid_scope_keys.append(f"{row['task_id']}:{row['scope_key']}")
    for row in database.execute(
        "SELECT task_id, scope_key FROM scope_locks ORDER BY task_id, scope_key"
    ):
        try:
            parse_scope_key(row["scope_key"])
        except LoopError:
            invalid_scope_keys.append(f"{row['task_id']}:{row['scope_key']}")
    if invalid_scope_keys:
        errors.append("scope_key 无效: " + ",".join(invalid_scope_keys))
    invalid_scope_modes = (
        database.execute(
            "SELECT t.id, s.scope_key FROM tasks t JOIN task_scopes s ON s.task_id=t.id "
            "WHERE t.preflight_status='READY' AND ("
            "(t.lock_mode='project' AND s.scope_key NOT LIKE 'project:%' "
            "AND s.scope_key NOT LIKE 'external:%') OR "
            "(t.lock_mode='module' AND s.scope_key NOT LIKE 'module:%') OR "
            "(t.lock_mode='file' AND s.scope_key NOT LIKE 'file:%')) "
            "ORDER BY t.id, s.ordinal"
        ).fetchall()
        if uses_preflight_schema(database)
        else []
    )
    if invalid_scope_modes:
        errors.append(
            "任务 lock_mode 与 scope_key 不一致: "
            + ",".join(
                f"{row['id']}:{row['scope_key']}" for row in invalid_scope_modes
            )
        )
    locks_without_scope = database.execute(
        "SELECT l.execution_id, l.scope_key FROM scope_locks l WHERE NOT EXISTS ("
        "SELECT 1 FROM task_scopes s WHERE s.task_id=l.task_id AND s.scope_key=l.scope_key) "
        "ORDER BY l.execution_id, l.scope_key"
    ).fetchall()
    if locks_without_scope:
        errors.append(
            "scope 锁缺少任务范围凭证: "
            + ",".join(
                f"{row['execution_id']}:{row['scope_key']}"
                for row in locks_without_scope
            )
        )
    active_locks = [
        dict(row)
        for row in database.execute(
            "SELECT scope_key, task_id, execution_id FROM scope_locks ORDER BY scope_key"
        ).fetchall()
    ]
    overlapping_locks: list[str] = []
    for index, left in enumerate(active_locks):
        for right in active_locks[index + 1 :]:
            if left["execution_id"] == right["execution_id"]:
                continue
            if scope_keys_conflict(left["scope_key"], right["scope_key"]):
                overlapping_locks.append(
                    f"{left['execution_id']}:{left['scope_key']}<->"
                    f"{right['execution_id']}:{right['scope_key']}"
                )
    if overlapping_locks:
        errors.append(
            "活动 scope 锁互相冲突: " + ",".join(overlapping_locks)
        )
    return {
        "ok": not errors,
        "schema_version": version,
        "tasks": database.execute("SELECT count(*) FROM tasks").fetchone()[0],
        "active_executions": active,
        "global_max_active_executions": maximum,
        "tables": sorted(actual_tables),
        "errors": errors,
    }
