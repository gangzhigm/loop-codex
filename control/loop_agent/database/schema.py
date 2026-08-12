"""SQLite 连接、空 Schema 创建和版本迁移。

运行时任务操作不应包含 Schema 改写逻辑。初始化无法创建数据库或迁移现有数据库时，
本模块是唯一排查入口。每次迁移都使用显式事务，校验外键和 quick_check，并在结束后恢复
外键约束。
"""

from __future__ import annotations

# 中文排查：连接建立、建表、逐版本迁移和事务辅助函数都在本模块。
# 迁移故障先确认当前 user_version、活动 execution 限制和目标版本的每一步 DDL。
# 每个迁移步骤必须可在同一事务中回滚；不要先改 user_version 再执行数据修复。

import re
import sqlite3
from pathlib import Path
from typing import Any

from loop_agent.configuration import (
    load_initialization_config,
    normalize_execution_target,
    resolve_execution_profile,
)
from loop_agent.constants import (
    ARCHIVE_SCHEMA_USER_VERSION,
    DIAGNOSTIC_SCHEMA_USER_VERSION,
    LEGACY_SCHEMA_USER_VERSION,
    LOCK_MODES,
    PREFLIGHT_SCHEMA_USER_VERSION,
    PROFILE_ROUTING_SCHEMA_USER_VERSION,
    RECOVERY_SCHEMA_USER_VERSION,
    ROUTING_SCHEMA_USER_VERSION,
    SCHEMA_USER_VERSION,
    SCHEMA_VERSION,
)
from loop_agent.database.compatibility import LEGACY_PROFILE_TO_CAPABILITY
from loop_agent.database.sql import (
    EXECUTIONS_TABLE_SQL,
    PREFLIGHT_EXECUTIONS_TABLE_SQL,
    SCOPE_LOCKS_TABLE_SQL,
    TASKS_TABLE_SQL,
)
from loop_agent.errors import LoopError
from loop_agent.paths import DEFAULT_DB, SCHEMA_PATH
from loop_agent.serialization import expires_at, json_dump, now_shanghai


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(str(database_path), timeout=5.0, isolation_level=None)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys = ON")
    database.execute("PRAGMA journal_mode = WAL")
    database.execute("PRAGMA busy_timeout = 5000")
    database.execute("PRAGMA synchronous = NORMAL")
    return database


def initialize_schema(database: sqlite3.Connection) -> None:
    current = int(database.execute("PRAGMA user_version").fetchone()[0])
    if current not in {0, SCHEMA_USER_VERSION}:
        raise LoopError(
            f"数据库 Schema 不是当前版本: user_version={current}；请先运行 loopctl.py migrate"
        )
    database.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def _create_current_execution_indexes(database: sqlite3.Connection) -> None:
    database.execute("CREATE INDEX idx_executions_active ON executions(status, lease_expires_at)")
    database.execute(
        "CREATE UNIQUE INDEX idx_executions_one_active_task "
        "ON executions(task_id) WHERE status='RUNNING'"
    )


def _create_preflight_schema_objects(database: sqlite3.Connection) -> None:
    database.execute(
        "CREATE TABLE task_technical_acceptance ("
        "task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, "
        "ordinal INTEGER NOT NULL, text TEXT NOT NULL, PRIMARY KEY(task_id, ordinal))"
    )
    database.execute(
        "CREATE TABLE task_preflight_evidence ("
        "task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, "
        "ordinal INTEGER NOT NULL, text TEXT NOT NULL, PRIMARY KEY(task_id, ordinal))"
    )
    database.execute(PREFLIGHT_EXECUTIONS_TABLE_SQL)
    database.execute(
        "CREATE INDEX idx_preflight_executions_active "
        "ON preflight_executions(status, lease_expires_at, attempt_deadline_at)"
    )
    database.execute(
        "CREATE UNIQUE INDEX idx_preflight_one_active_task "
        "ON preflight_executions(task_id) WHERE status='INSPECTING'"
    )


def _migrate_preflight_schema(database: sqlite3.Connection, source_version: str = "3.6.0") -> dict[str, Any]:
    task_rows = [dict(row) for row in database.execute("SELECT * FROM tasks ORDER BY id").fetchall()]
    scopes = {
        row["task_id"]: [] for row in database.execute("SELECT DISTINCT task_id FROM task_scopes").fetchall()
    }
    for row in database.execute("SELECT task_id, scope FROM task_scopes ORDER BY task_id, ordinal"):
        scopes.setdefault(row["task_id"], []).append(row["scope"])
    active = int(database.execute("SELECT count(*) FROM executions WHERE status='RUNNING'").fetchone()[0])
    stamp = now_shanghai()
    old_drafts = 0
    ready = 0
    ready_ids: set[str] = set()
    database.execute("PRAGMA foreign_keys = OFF")
    try:
        transaction(database)
        database.execute(TASKS_TABLE_SQL)
        for row in task_rows:
            was_draft = row["status"] == "DRAFT"
            preflight_ready = not was_draft and bool(scopes.get(row["id"], []))
            status = "NEEDS_REVIEW" if was_draft else row["status"]
            preflight_status = "READY" if preflight_ready else "FAILED"
            failure = (
                "Schema 3.7.0 迁移：旧 DRAFT 的边界未经过 Planner 预检，需要人工补充后重新预检。"
                if was_draft else (
                    "Schema 3.7.0 迁移：历史终态缺少精确 scope，未声明为 READY。"
                    if not preflight_ready else None
                )
            )
            estimated = row.get("capability_level")
            capability = None if was_draft else row.get("capability_level")
            scope_hint = json_dump(scopes.get(row["id"], []))
            values = (
                row["id"], row["title"], row.get("description", ""), status, row["priority"],
                estimated, capability, row["runtime_environment"], row.get("provider_id"),
                row["execution_policy"], preflight_status, None, None,
                stamp if preflight_ready else None, failure, scope_hint,
                "project" if preflight_ready else None, "[]", row.get("assigned_agent"),
                row["created_at"], row.get("started_at"), stamp if was_draft else row["updated_at"],
                row.get("heartbeat_at"), row.get("completed_at"), row.get("archived_at"),
                row.get("attempt", 0), row.get("progress_percent", 0),
                failure if was_draft else row.get("progress_summary", ""),
                "补充原始任务定义后重新进入 Planner 预检。" if was_draft else row.get("progress_next_step"),
                row.get("result_summary"), row.get("result_error"), row.get("result_diagnostic_json"),
                1 if was_draft else row.get("human_required", 0),
                (row.get("human_question") or failure) if was_draft else row.get("human_question"),
                (row.get("human_options_json") if row.get("human_options_json") not in {None, "[]"}
                 else json_dump(["补充任务定义并重新预检", "取消任务"])) if was_draft
                else row.get("human_options_json", "[]"),
                stamp if was_draft else row.get("human_requested_at"),
                row.get("human_responded_at"), row.get("human_response"),
                int(row.get("row_version", 1)) + (1 if was_draft else 0),
            )
            database.execute(
                """INSERT INTO tasks_new(
                  id, title, description, status, priority, estimated_capability_level, capability_level,
                  runtime_environment, provider_id, execution_policy, preflight_status,
                  preflight_execution_id, preflight_started_at, preflight_completed_at, preflight_failure,
                  scope_hint_json, lock_mode, split_suggestions_json, assigned_agent, created_at, started_at,
                  updated_at, heartbeat_at, completed_at, archived_at, attempt, progress_percent,
                  progress_summary, progress_next_step, result_summary, result_error, result_diagnostic_json,
                  human_required, human_question, human_options_json, human_requested_at,
                  human_responded_at, human_response, row_version
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                         ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
            if was_draft:
                old_drafts += 1
                database.execute(
                    "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
                    "VALUES(?, ?, 'DRAFT', 'NEEDS_REVIEW', 'schema-migration', ?)",
                    (row["id"], stamp, failure),
                )
            else:
                if preflight_ready:
                    ready += 1
                    ready_ids.add(row["id"])
        database.execute("DROP TABLE tasks")
        database.execute("ALTER TABLE tasks_new RENAME TO tasks")
        database.execute(
            "CREATE INDEX idx_tasks_queue ON tasks(status, preflight_status, runtime_environment, provider_id, "
            "capability_level, execution_policy, priority, created_at, id)"
        )
        database.execute("CREATE INDEX idx_tasks_preflight ON tasks(status, preflight_status, priority, created_at, id)")
        database.execute("CREATE INDEX idx_tasks_archived ON tasks(archived_at, status, updated_at)")
        _create_preflight_schema_objects(database)
        for row in task_rows:
            if row["id"] in ready_ids:
                database.execute(
                    "INSERT INTO task_technical_acceptance(task_id, ordinal, text) VALUES(?, 0, ?)",
                    (row["id"], "Schema 3.7.0 迁移：沿用既有业务验收与已执行契约。"),
                )
                database.execute(
                    "INSERT INTO task_preflight_evidence(task_id, ordinal, text) VALUES(?, 0, ?)",
                    (row["id"], "Schema 3.7.0 迁移：既有非 DRAFT 任务按 READY 保持队列连续性。"),
                )
        execution_columns = {
            row[1] for row in database.execute("PRAGMA table_info(executions)").fetchall()
        }
        if "execution_kind" not in execution_columns:
            database.execute(
                "ALTER TABLE executions ADD COLUMN execution_kind TEXT NOT NULL DEFAULT 'WORKER' "
                "CHECK (execution_kind = 'WORKER')"
            )
        database.execute(f"PRAGMA user_version = {SCHEMA_USER_VERSION}")
        foreign_key_errors = database.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise LoopError(f"Schema 迁移产生外键错误: {len(foreign_key_errors)}")
        if database.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise LoopError("Schema 迁移后 quick_check 失败")
        commit(database)
    except Exception:
        rollback(database)
        raise
    finally:
        database.execute("PRAGMA foreign_keys = ON")
    return {
        "from": source_version,
        "to": SCHEMA_VERSION,
        "migrated": True,
        "old_drafts_moved_to_review": old_drafts,
        "tasks_marked_ready": ready,
        "active_executions_preserved": active,
    }


def _migrate_recovery_schema(database: sqlite3.Connection) -> dict[str, Any]:
    execution_rows = [
        dict(row) for row in database.execute("SELECT * FROM executions ORDER BY execution_id").fetchall()
    ]
    lock_rows = [
        dict(row) for row in database.execute("SELECT * FROM scope_locks ORDER BY scope_key").fetchall()
    ]
    active = sum(row["status"] == "RUNNING" for row in execution_rows)
    database.execute("PRAGMA foreign_keys = OFF")
    try:
        transaction(database)
        database.execute("ALTER TABLE tasks ADD COLUMN result_diagnostic_json TEXT")
        database.execute("DROP TABLE scope_locks")
        database.execute(EXECUTIONS_TABLE_SQL)
        for row in execution_rows:
            database.execute(
                """INSERT INTO executions_new(
                  execution_id, task_id, status, started_at, heartbeat_at, lease_expires_at, finished_at,
                  outcome, runtime_environment, provider_id, capability_level, execution_policy, model,
                  reasoning, attempt_timeout_seconds, max_retries, termination_reason, recovery_required,
                  recovered_at, recovery_action
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL, NULL)""",
                (
                    row["execution_id"], row["task_id"], row["status"], row["started_at"],
                    row["heartbeat_at"], row["lease_expires_at"], row.get("finished_at"),
                    row.get("outcome"), row["runtime_environment"], row.get("provider_id"),
                    row["capability_level"], row["execution_policy"], row["model"], row["reasoning"],
                    row["attempt_timeout_seconds"], row["max_retries"],
                ),
            )
        database.execute("DROP TABLE executions")
        database.execute("ALTER TABLE executions_new RENAME TO executions")
        database.execute(SCOPE_LOCKS_TABLE_SQL)
        for row in lock_rows:
            database.execute(
                """INSERT INTO scope_locks_new(
                  scope_key, task_id, execution_id, acquired_at, lease_expires_at, status
                ) VALUES(?, ?, ?, ?, ?, 'ACTIVE')""",
                (
                    row["scope_key"], row["task_id"], row["execution_id"],
                    row["acquired_at"], row["lease_expires_at"],
                ),
            )
        database.execute("ALTER TABLE scope_locks_new RENAME TO scope_locks")
        _create_current_execution_indexes(database)
        database.execute(f"PRAGMA user_version = {PREFLIGHT_SCHEMA_USER_VERSION}")
        foreign_key_errors = database.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise LoopError(f"Schema 迁移产生外键错误: {len(foreign_key_errors)}")
        if database.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise LoopError("Schema 迁移后 quick_check 失败")
        commit(database)
    except Exception:
        rollback(database)
        raise
    finally:
        database.execute("PRAGMA foreign_keys = ON")
    return {
        "from": "3.4.0",
        "to": "3.6.0",
        "migrated": True,
        "active_executions_preserved": active,
        "scope_locks_preserved": len(lock_rows),
        "quarantines_created": 0,
    }


def _migrate_diagnostic_schema(database: sqlite3.Connection) -> dict[str, Any]:
    active = int(database.execute("SELECT count(*) FROM executions WHERE status='RUNNING'").fetchone()[0])
    try:
        transaction(database)
        database.execute("ALTER TABLE tasks ADD COLUMN result_diagnostic_json TEXT")
        database.execute(f"PRAGMA user_version = {PREFLIGHT_SCHEMA_USER_VERSION}")
        if database.execute("PRAGMA foreign_key_check").fetchall():
            raise LoopError("Schema 迁移产生外键错误")
        if database.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise LoopError("Schema 迁移后 quick_check 失败")
        commit(database)
    except Exception:
        rollback(database)
        raise
    return {
        "from": "3.5.0",
        "to": "3.6.0",
        "migrated": True,
        "active_executions_preserved": active,
    }


def uses_hybrid_scope_schema(database: sqlite3.Connection) -> bool:
    row = database.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
    ).fetchone()
    if row is None or not row[0]:
        return False
    match = re.search(
        r"lock_mode\s+TEXT\s+CHECK\s*\(lock_mode\s+IS\s+NULL\s+OR\s+lock_mode\s+IN\s*\(([^)]*)\)",
        row[0],
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        return False
    return set(re.findall(r"'([^']+)'", match.group(1))) == set(LOCK_MODES)


def _migrate_hybrid_scope_schema(database: sqlite3.Connection) -> dict[str, Any]:
    active = int(database.execute("SELECT count(*) FROM executions WHERE status='RUNNING'").fetchone()[0])
    old_conflicts = [
        row[0] for row in database.execute(
            "SELECT id FROM tasks WHERE status='WAITING_CONFLICT' ORDER BY id"
        ).fetchall()
    ]
    stamp = now_shanghai()
    database.execute("PRAGMA foreign_keys = OFF")
    try:
        transaction(database)
        database.execute(TASKS_TABLE_SQL)
        database.execute("INSERT INTO tasks_new SELECT * FROM tasks")
        database.execute("DROP TABLE tasks")
        database.execute("ALTER TABLE tasks_new RENAME TO tasks")
        database.execute(
            "CREATE INDEX idx_tasks_queue ON tasks(status, preflight_status, runtime_environment, provider_id, "
            "capability_level, execution_policy, priority, created_at, id)"
        )
        database.execute(
            "CREATE INDEX idx_tasks_preflight ON tasks(status, preflight_status, priority, created_at, id)"
        )
        database.execute("CREATE INDEX idx_tasks_archived ON tasks(archived_at, status, updated_at)")
        for task_id in old_conflicts:
            database.execute(
                "UPDATE tasks SET status='PENDING', updated_at=?, "
                "progress_summary='旧 WAITING_CONFLICT 已转换为动态 scope 排队。', "
                "progress_next_step='等待 scope 可用后由下一次 claim 自动领取。', "
                "row_version=row_version+1 WHERE id=?",
                (stamp, task_id),
            )
            database.execute(
                "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
                "VALUES(?, ?, 'WAITING_CONFLICT', 'PENDING', 'schema-migration', "
                "'混合 scope 锁迁移：保留原 blocker 记录，改用 PENDING 动态排队。')",
                (task_id, stamp),
            )
        if database.execute("PRAGMA foreign_key_check").fetchall():
            raise LoopError("混合 scope 锁迁移产生外键错误")
        if database.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise LoopError("混合 scope 锁迁移后 quick_check 失败")
        commit(database)
    except Exception:
        rollback(database)
        raise
    finally:
        database.execute("PRAGMA foreign_keys = ON")
    return {
        "from": SCHEMA_VERSION,
        "to": SCHEMA_VERSION,
        "migrated": True,
        "hybrid_scope_lock_migrated": True,
        "waiting_conflicts_requeued": old_conflicts,
        "active_executions_preserved": active,
    }


def migrate_schema(database: sqlite3.Connection) -> dict[str, Any]:
    current = int(database.execute("PRAGMA user_version").fetchone()[0])
    if current == SCHEMA_USER_VERSION:
        if not uses_hybrid_scope_schema(database):
            return _migrate_hybrid_scope_schema(database)
        return {
            "from": SCHEMA_VERSION, "to": SCHEMA_VERSION, "migrated": False,
            "archived": 0, "tasks_mapped": 0, "executions_snapshotted": 0,
        }
    if current == PREFLIGHT_SCHEMA_USER_VERSION:
        return _migrate_preflight_schema(database)
    if current == DIAGNOSTIC_SCHEMA_USER_VERSION:
        first = _migrate_diagnostic_schema(database)
        return {**_migrate_preflight_schema(database, first["from"]), **{
            key: value for key, value in first.items() if key not in {"from", "to", "migrated"}
        }}
    if current == RECOVERY_SCHEMA_USER_VERSION:
        first = _migrate_recovery_schema(database)
        return {**_migrate_preflight_schema(database, first["from"]), **{
            key: value for key, value in first.items() if key not in {"from", "to", "migrated"}
        }}
    supported_versions = {
        LEGACY_SCHEMA_USER_VERSION,
        ARCHIVE_SCHEMA_USER_VERSION,
        ROUTING_SCHEMA_USER_VERSION,
        PROFILE_ROUTING_SCHEMA_USER_VERSION,
    }
    if current not in supported_versions:
        raise LoopError(
            "不支持的 Schema 迁移: "
            f"user_version={current}, expected one of {sorted(supported_versions)}"
        )
    active = int(database.execute("SELECT count(*) FROM executions WHERE status='RUNNING'").fetchone()[0])
    if active:
        raise LoopError(f"Schema 迁移要求没有活动 execution，当前为 {active}")

    source_version = {
        LEGACY_SCHEMA_USER_VERSION: "3.0.0",
        ARCHIVE_SCHEMA_USER_VERSION: "3.1.0",
        ROUTING_SCHEMA_USER_VERSION: "3.2.0",
        PROFILE_ROUTING_SCHEMA_USER_VERSION: "3.3.0",
    }[current]
    config = load_initialization_config()
    database.execute("PRAGMA foreign_keys = OFF")
    try:
        transaction(database)
        columns = {row[1] for row in database.execute("PRAGMA table_info(tasks)").fetchall()}
        if "archived_at" not in columns:
            database.execute("ALTER TABLE tasks ADD COLUMN archived_at TEXT")
        confirmed = database.execute(
            "SELECT id, status, COALESCE(completed_at, updated_at, created_at) AS archived_at "
            "FROM tasks WHERE status='CONFIRMED' AND archived_at IS NULL"
        ).fetchall() if current == LEGACY_SCHEMA_USER_VERSION else []
        reason = "Schema 3.1.0 迁移：按旧版 CONFIRMED 等同已归档的语义设置 archived_at。"
        for task in confirmed:
            database.execute(
                "UPDATE tasks SET archived_at=?, row_version=row_version+1 WHERE id=?",
                (task["archived_at"], task["id"]),
            )
            database.execute(
                "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
                "VALUES(?, ?, ?, ?, 'schema-migration', ?)",
                (task["id"], now_shanghai(), task["status"], task["status"], reason),
            )

        task_rows = [dict(row) for row in database.execute("SELECT * FROM tasks ORDER BY id").fetchall()]
        execution_rows = [
            dict(row) for row in database.execute("SELECT * FROM executions ORDER BY execution_id").fetchall()
        ]
        normalized_tasks: dict[str, dict[str, Any]] = {}
        for row in task_rows:
            legacy_profile = row.get("execution_profile", "standard")
            legacy_environment = row.get("runtime_environment", "codex_automation")
            if legacy_environment == "deepseek":
                runtime_environment, provider_id = "self_hosted_agent", "deepseek"
            else:
                runtime_environment, provider_id = normalize_execution_target(legacy_environment)
            normalized_tasks[row["id"]] = {
                "capability_level": LEGACY_PROFILE_TO_CAPABILITY[legacy_profile],
                "runtime_environment": runtime_environment,
                "provider_id": provider_id,
                "execution_policy": "manual" if legacy_profile == "exceptional" else "automatic",
            }

        database.execute(TASKS_TABLE_SQL)
        draft_migrations: list[tuple[str, str]] = []
        legacy_ready_ids: set[str] = set()
        for row in task_rows:
            route = normalized_tasks[row["id"]]
            was_draft = row["status"] == "DRAFT"
            status = "NEEDS_REVIEW" if was_draft else row["status"]
            scope_hint = [
                item[0] for item in database.execute(
                    "SELECT scope FROM task_scopes WHERE task_id=? ORDER BY ordinal", (row["id"],)
                ).fetchall()
            ]
            preflight_ready = not was_draft and bool(scope_hint)
            failure = (
                "Schema 3.7.0 迁移：旧 DRAFT 的边界未经过 Planner 预检，需要人工补充后重新预检。"
                if was_draft else (
                    "Schema 3.7.0 迁移：历史终态缺少精确 scope，未声明为 READY。"
                    if not preflight_ready else None
                )
            )
            database.execute(
                """INSERT INTO tasks_new(
                  id, title, description, status, priority, estimated_capability_level, capability_level,
                  runtime_environment, provider_id, execution_policy, preflight_status,
                  preflight_execution_id, preflight_started_at, preflight_completed_at, preflight_failure,
                  scope_hint_json, lock_mode, split_suggestions_json, assigned_agent, created_at, started_at,
                  updated_at, heartbeat_at, completed_at, archived_at, attempt, progress_percent,
                  progress_summary, progress_next_step, result_summary, result_error, result_diagnostic_json,
                  human_required, human_question, human_options_json, human_requested_at,
                  human_responded_at, human_response, row_version
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                         ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["id"], row["title"], row.get("description", ""), status, row["priority"],
                    route["capability_level"], None if was_draft else route["capability_level"],
                    route["runtime_environment"], route["provider_id"], route["execution_policy"],
                    "READY" if preflight_ready else "FAILED", None, None,
                    now_shanghai() if preflight_ready else None, failure, json_dump(scope_hint),
                    "project" if preflight_ready else None, "[]", row.get("assigned_agent"), row["created_at"],
                    row.get("started_at"), row["updated_at"], row.get("heartbeat_at"),
                    row.get("completed_at"), row.get("archived_at"), row.get("attempt", 0),
                    row.get("progress_percent", 0), failure if was_draft else row.get("progress_summary", ""),
                    "补充原始任务定义后重新进入 Planner 预检。" if was_draft else row.get("progress_next_step"),
                    row.get("result_summary"), row.get("result_error"), None,
                    1 if was_draft else row.get("human_required", 0),
                    (row.get("human_question") or failure) if was_draft else row.get("human_question"),
                    json_dump(["补充任务定义并重新预检", "取消任务"]) if was_draft
                    else row.get("human_options_json", "[]"),
                    now_shanghai() if was_draft else row.get("human_requested_at"),
                    row.get("human_responded_at"), row.get("human_response"),
                    int(row.get("row_version", 1)) + (1 if was_draft else 0),
                ),
            )
            if was_draft:
                draft_migrations.append((row["id"], failure))
            elif preflight_ready:
                legacy_ready_ids.add(row["id"])
        database.execute(EXECUTIONS_TABLE_SQL)
        for row in execution_rows:
            route = normalized_tasks[row["task_id"]]
            snapshot = resolve_execution_profile(
                route["runtime_environment"], route["provider_id"], route["capability_level"], config
            )
            database.execute(
                """INSERT INTO executions_new(
                  execution_id, task_id, status, started_at, heartbeat_at, lease_expires_at, finished_at,
                  outcome, runtime_environment, provider_id, capability_level, execution_policy, model,
                  reasoning, attempt_timeout_seconds, max_retries
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["execution_id"], row["task_id"], row["status"], row["started_at"],
                    row["heartbeat_at"], row["lease_expires_at"], row.get("finished_at"), row.get("outcome"),
                    snapshot["runtime_environment"], snapshot["provider_id"], snapshot["capability_level"],
                    route["execution_policy"], snapshot["model"], snapshot["reasoning"],
                    snapshot["attempt_timeout_seconds"], snapshot["max_retries"],
                ),
            )
        database.execute("DROP TABLE executions")
        database.execute("DROP TABLE tasks")
        database.execute("ALTER TABLE tasks_new RENAME TO tasks")
        database.execute("ALTER TABLE executions_new RENAME TO executions")
        database.execute(
            "CREATE INDEX idx_tasks_queue ON tasks(status, preflight_status, runtime_environment, provider_id, "
            "capability_level, execution_policy, priority, created_at, id)"
        )
        database.execute("CREATE INDEX idx_tasks_preflight ON tasks(status, preflight_status, priority, created_at, id)")
        database.execute(
            "CREATE INDEX idx_tasks_archived ON tasks(archived_at, status, updated_at)"
        )
        database.execute(
            "ALTER TABLE scope_locks ADD COLUMN status TEXT NOT NULL DEFAULT 'ACTIVE' "
            "CHECK (status IN ('ACTIVE', 'QUARANTINED'))"
        )
        database.execute("ALTER TABLE scope_locks ADD COLUMN quarantined_at TEXT")
        database.execute("ALTER TABLE scope_locks ADD COLUMN quarantine_reason TEXT")
        _create_current_execution_indexes(database)
        _create_preflight_schema_objects(database)
        for row in task_rows:
            if row["id"] in legacy_ready_ids:
                database.execute(
                    "INSERT INTO task_technical_acceptance(task_id, ordinal, text) VALUES(?, 0, ?)",
                    (row["id"], "Schema 3.7.0 迁移：沿用既有业务验收与已执行契约。"),
                )
                database.execute(
                    "INSERT INTO task_preflight_evidence(task_id, ordinal, text) VALUES(?, 0, ?)",
                    (row["id"], "Schema 3.7.0 迁移：既有非 DRAFT 任务按 READY 保持队列连续性。"),
                )
        for task_id, failure in draft_migrations:
            database.execute(
                "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
                "VALUES(?, ?, 'DRAFT', 'NEEDS_REVIEW', 'schema-migration', ?)",
                (task_id, now_shanghai(), failure),
            )
        database.execute(f"PRAGMA user_version = {SCHEMA_USER_VERSION}")
        foreign_key_errors = database.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise LoopError(f"Schema 迁移产生外键错误: {len(foreign_key_errors)}")
        if database.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise LoopError("Schema 迁移后 quick_check 失败")
        commit(database)
    except Exception:
        rollback(database)
        raise
    finally:
        database.execute("PRAGMA foreign_keys = ON")
    return {
        "from": source_version,
        "to": SCHEMA_VERSION,
        "migrated": True,
        "archived": len(confirmed),
        "tasks_mapped": len(task_rows),
        "executions_snapshotted": len(execution_rows),
        "old_drafts_moved_to_review": len(draft_migrations),
        "tasks_marked_ready": len(legacy_ready_ids),
    }


def schema_version(database: sqlite3.Connection) -> str:
    value = int(database.execute("PRAGMA user_version").fetchone()[0])
    return SCHEMA_VERSION if value == SCHEMA_USER_VERSION else str(value)


def transaction(database: sqlite3.Connection) -> None:
    database.execute("BEGIN IMMEDIATE")


def commit(database: sqlite3.Connection) -> None:
    database.execute("COMMIT")


def rollback(database: sqlite3.Connection) -> None:
    if database.in_transaction:
        database.execute("ROLLBACK")
