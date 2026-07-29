from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = BASE_DIR / "loop-agent.sqlite3"
SCHEMA_PATH = BASE_DIR / "schemas" / "loop-agent.sql"
CONFIG_PATH = BASE_DIR / "config" / "initialization.json"
SCHEMA_VERSION = "2.0.0"
SHANGHAI = timezone(timedelta(hours=8))
PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
FINAL_EXECUTION_STATUSES = {"SUCCEEDED", "FAILED", "WAITING_HUMAN"}
DEPENDENCY_COMPLETE_STATUSES = {"SUCCEEDED", "CONFIRMED"}
FORBIDDEN_SCOPE_ROOTS = {"$CODEX_HOME", ".reasonix", ".env"}


class LoopError(RuntimeError):
    pass


def load_initialization_config(path: Path | str = CONFIG_PATH) -> dict[str, Any]:
    config_path = Path(path).resolve()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LoopError(f"初始化配置无效: {config_path}: {error}") from error
    dashboard = config.get("dashboard") or {}
    automations = config.get("automations") or {}
    health = config.get("health") or {}
    valid = (
        config.get("config_version") == "1.0.0"
        and isinstance(dashboard.get("host"), str)
        and isinstance(dashboard.get("port"), int)
        and 1 <= dashboard["port"] <= 65535
        and isinstance(dashboard.get("poll_interval_ms"), int)
        and dashboard["poll_interval_ms"] >= 500
        and isinstance(automations.get("worker_interval_minutes"), int)
        and automations["worker_interval_minutes"] >= 1
        and isinstance(automations.get("health_interval_minutes"), int)
        and automations["health_interval_minutes"] >= 1
        and isinstance(health.get("failure_threshold"), int)
        and health["failure_threshold"] >= 1
    )
    if not valid:
        raise LoopError(f"初始化配置字段或取值无效: {config_path}")
    return config


def now_shanghai() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="milliseconds")


def expires_at(seconds: int) -> str:
    return (datetime.now(SHANGHAI) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_load(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    database = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys = ON")
    database.execute("PRAGMA journal_mode = WAL")
    database.execute("PRAGMA busy_timeout = 5000")
    database.execute("PRAGMA synchronous = NORMAL")
    return database


def initialize_schema(database: sqlite3.Connection) -> None:
    database.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    stamp = now_shanghai()
    database.execute(
        "INSERT INTO metadata(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (SCHEMA_VERSION,),
    )
    database.execute(
        "INSERT INTO metadata(key, value) VALUES('initialized_at', ?) ON CONFLICT(key) DO NOTHING",
        (stamp,),
    )


def transaction(database: sqlite3.Connection) -> None:
    database.execute("BEGIN IMMEDIATE")


def commit(database: sqlite3.Connection) -> None:
    database.execute("COMMIT")


def rollback(database: sqlite3.Connection) -> None:
    if database.in_transaction:
        database.execute("ROLLBACK")


def setting(database: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = database.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
    return json_load(row[0], default) if row else default


def set_setting(database: sqlite3.Connection, key: str, value: Any) -> None:
    database.execute(
        "INSERT INTO settings(key, value_json) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
        (key, json_dump(value)),
    )


def metadata(database: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = database.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_metadata(database: sqlite3.Connection, key: str, value: str) -> None:
    database.execute(
        "INSERT INTO metadata(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def parse_project_registry(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    projects: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = re.match(r"^\|\s*`([^`]+)`\s*\|\s*(.*?)\s*\|\s*$", line)
        if not match or match.group(1) == "文件夹名":
            continue
        relative = match.group(1).replace("\\", "/").strip("/")
        projects.append(
            {
                "path": relative,
                "description": match.group(2).strip(),
                "exists_on_disk": int((path.parent / Path(relative)).exists()),
            }
        )
    if not projects:
        raise LoopError(f"项目清单没有可解析项目: {path}")
    return projects


def refresh_projects(database: sqlite3.Connection, registry_path: Path) -> list[dict[str, Any]]:
    stamp = now_shanghai()
    projects = parse_project_registry(registry_path)
    for project in projects:
        database.execute(
            "INSERT INTO projects(path, description, exists_on_disk, updated_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET description=excluded.description, "
            "exists_on_disk=excluded.exists_on_disk, updated_at=excluded.updated_at",
            (project["path"], project["description"], project["exists_on_disk"], stamp),
        )
    return projects


def resolve_scope_key(database: sqlite3.Connection, scope: str) -> str:
    normalized = scope.replace("\\", "/").strip()
    if not normalized or ".." in normalized.split("/"):
        raise LoopError(f"不安全的 scope: {scope}")
    if normalized.upper().startswith("OSS:"):
        return f"external:{normalized}"
    normalized = normalized.strip("/")
    if normalized.split("/", 1)[0] in FORBIDDEN_SCOPE_ROOTS:
        raise LoopError(f"禁止的 scope: {scope}")
    rows = database.execute("SELECT path FROM projects ORDER BY length(path) DESC").fetchall()
    for row in rows:
        project = row[0].replace("\\", "/").strip("/")
        if normalized == project or normalized.startswith(f"{project}/"):
            return f"project:{project}"
    raise LoopError(f"scope 未匹配项目清单: {scope}")


def replace_ordered_text(
    database: sqlite3.Connection, table: str, task_id: str, values: Iterable[str]
) -> None:
    database.execute(f"DELETE FROM {table} WHERE task_id=?", (task_id,))
    database.executemany(
        f"INSERT INTO {table}(task_id, ordinal, text) VALUES(?, ?, ?)",
        [(task_id, index, str(value)) for index, value in enumerate(values)],
    )


def task_exists(database: sqlite3.Connection, task_id: str) -> bool:
    return database.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is not None


def insert_task(database: sqlite3.Connection, task: dict[str, Any], actor: str = "migration") -> None:
    task_id = task.get("id")
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Z][A-Z0-9_-]*", task_id):
        raise LoopError(f"任务 id 无效: {task_id}")
    if task_exists(database, task_id):
        raise LoopError(f"任务 id 已存在: {task_id}")
    status = task.get("status", "PENDING")
    stamp = task.get("created_at") or now_shanghai()
    progress = task.get("progress") or {}
    result = task.get("result") or {}
    human = task.get("human_intervention") or {}
    database.execute(
        """
        INSERT INTO tasks(
          id, title, description, status, priority, assigned_agent,
          created_at, started_at, updated_at, heartbeat_at, completed_at, attempt,
          progress_percent, progress_summary, progress_next_step,
          result_summary, result_error,
          human_required, human_question, human_options_json,
          human_requested_at, human_responded_at, human_response, row_version
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            str(task.get("title") or task_id),
            str(task.get("description") or ""),
            status,
            task.get("priority", "medium"),
            task.get("assigned_agent"),
            stamp,
            task.get("started_at"),
            task.get("updated_at") or stamp,
            task.get("heartbeat_at"),
            task.get("completed_at"),
            int(task.get("attempt", 0)),
            int(progress.get("percent", 0)),
            str(progress.get("summary") or ""),
            progress.get("next_step"),
            result.get("summary"),
            result.get("error"),
            int(bool(human.get("required", False))),
            human.get("question"),
            json_dump(human.get("options") or []),
            human.get("requested_at"),
            human.get("responded_at"),
            human.get("response"),
            1,
        ),
    )
    for dependency in task.get("depends_on") or []:
        database.execute(
            "INSERT INTO task_dependencies(task_id, dependency_id) VALUES(?, ?)",
            (task_id, dependency),
        )
    for index, scope in enumerate(task.get("scope") or []):
        database.execute(
            "INSERT INTO task_scopes(task_id, ordinal, scope, scope_key) VALUES(?, ?, ?, ?)",
            (task_id, index, scope, resolve_scope_key(database, scope)),
        )
    replace_ordered_text(database, "task_acceptance", task_id, task.get("acceptance") or [])
    replace_ordered_text(database, "task_completed_items", task_id, progress.get("completed") or [])
    replace_ordered_text(database, "task_verifications", task_id, result.get("verification") or [])
    for index, attachment in enumerate(task.get("attachments") or []):
        database.execute(
            "INSERT INTO task_attachments(task_id, ordinal, path, sha256, role, saved_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (
                task_id,
                index,
                attachment.get("path"),
                attachment.get("sha256"),
                attachment.get("role", "source"),
                attachment.get("saved_at") or stamp,
            ),
        )
    history = task.get("history") or [
        {"at": stamp, "from": None, "to": status, "actor": actor, "reason": "任务已创建。"}
    ]
    for entry in history:
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (
                task_id,
                entry.get("at") or stamp,
                entry.get("from"),
                entry.get("to") or status,
                entry.get("actor") or actor,
                entry.get("reason") or "状态迁移。",
            ),
        )


def task_children(
    database: sqlite3.Connection,
    task_id: str,
    table: str,
    column: str = "text",
    order_column: str = "ordinal",
) -> list[Any]:
    rows = database.execute(
        f"SELECT {column} FROM {table} WHERE task_id=? ORDER BY {order_column}", (task_id,)
    ).fetchall()
    return [row[0] for row in rows]


def task_dict(database: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    task_id = row["id"]
    attachments = [
        dict(item)
        for item in database.execute(
            "SELECT path, sha256, role, saved_at FROM task_attachments WHERE task_id=? ORDER BY ordinal",
            (task_id,),
        ).fetchall()
    ]
    history = [
        {
            "at": item["at"],
            "from": item["from_status"],
            "to": item["to_status"],
            "actor": item["actor"],
            "reason": item["reason"],
        }
        for item in database.execute(
            "SELECT at, from_status, to_status, actor, reason FROM task_history "
            "WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()
    ]
    conflicts = [
        dict(item)
        for item in database.execute(
            "SELECT scope_key, blocker_task_id, blocker_execution_id, detected_at "
            "FROM task_conflicts WHERE task_id=? ORDER BY scope_key",
            (task_id,),
        ).fetchall()
    ]
    return {
        "id": task_id,
        "title": row["title"],
        "description": row["description"],
        "status": row["status"],
        "priority": row["priority"],
        "assigned_agent": row["assigned_agent"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "updated_at": row["updated_at"],
        "heartbeat_at": row["heartbeat_at"],
        "completed_at": row["completed_at"],
        "attempt": row["attempt"],
        "depends_on": task_children(
            database, task_id, "task_dependencies", "dependency_id", "dependency_id"
        ),
        "scope": task_children(database, task_id, "task_scopes", "scope"),
        "scope_keys": task_children(database, task_id, "task_scopes", "scope_key"),
        "acceptance": task_children(database, task_id, "task_acceptance"),
        "progress": {
            "percent": row["progress_percent"],
            "summary": row["progress_summary"],
            "completed": task_children(database, task_id, "task_completed_items"),
            "next_step": row["progress_next_step"],
        },
        "human_intervention": {
            "required": bool(row["human_required"]),
            "question": row["human_question"],
            "options": json_load(row["human_options_json"], []),
            "requested_at": row["human_requested_at"],
            "responded_at": row["human_responded_at"],
            "response": row["human_response"],
        },
        "attachments": attachments,
        "result": {
            "summary": row["result_summary"],
            "verification": task_children(database, task_id, "task_verifications"),
            "error": row["result_error"],
        },
        "history": history,
        "conflicts": conflicts,
        "row_version": row["row_version"],
    }


def all_tasks(database: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = database.execute(
        "SELECT * FROM tasks ORDER BY "
        "CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, "
        "created_at, id"
    ).fetchall()
    return [task_dict(database, row) for row in rows]


def state_payload(database: sqlite3.Connection) -> dict[str, Any]:
    settings_rows = database.execute("SELECT key, value_json FROM settings ORDER BY key").fetchall()
    settings_value = {row["key"]: json_load(row["value_json"], None) for row in settings_rows}
    executions = database.execute(
        "SELECT execution_id, task_id, heartbeat_at FROM executions WHERE status='RUNNING' ORDER BY started_at"
    ).fetchall()
    agents = [
        {
            "id": row["execution_id"],
            "role": "worker",
            "status": "RUNNING",
            "current_task_id": row["task_id"],
            "last_seen_at": row["heartbeat_at"],
            "summary": "Concurrent SQLite worker",
        }
        for row in executions
    ]
    services = [dict(row) for row in database.execute("SELECT * FROM service_state ORDER BY component").fetchall()]
    health_events = [
        {
            **dict(row),
            "details": json_load(row["details_json"], {}),
        }
        for row in database.execute(
            "SELECT at, component, status, message, details_json FROM health_events ORDER BY id DESC LIMIT 12"
        ).fetchall()
    ]
    projects = [dict(row) for row in database.execute("SELECT * FROM projects ORDER BY path").fetchall()]
    return {
        "schema_version": SCHEMA_VERSION,
        "workspace": {
            "name": metadata(database, "workspace_name", "Cross-Project Local Agent Loop"),
            "timezone": "Asia/Shanghai",
            "revision": int(metadata(database, "revision", "1") or 1),
            "updated_at": metadata(database, "updated_at", now_shanghai()),
            "writer": metadata(database, "writer", "sqlite"),
            "task_root": metadata(database, "task_root", r"E:\code"),
            "project_registry": metadata(database, "project_registry", "根目录清单.md"),
        },
        "settings": settings_value,
        "agents": agents,
        "tasks": all_tasks(database),
        "services": services,
        "health_events": health_events,
        "projects": projects,
    }


def bump_revision(database: sqlite3.Connection, writer: str) -> int:
    revision = int(metadata(database, "revision", "0") or 0) + 1
    set_metadata(database, "revision", str(revision))
    set_metadata(database, "updated_at", now_shanghai())
    set_metadata(database, "writer", writer)
    return revision


def validate_database(database: sqlite3.Connection) -> dict[str, Any]:
    errors: list[str] = []
    schema_version = metadata(database, "schema_version")
    if schema_version != SCHEMA_VERSION:
        errors.append(f"schema_version={schema_version}, expected={SCHEMA_VERSION}")
    foreign_keys = database.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        errors.append(f"foreign_key_check={len(foreign_keys)}")
    integrity = database.execute("PRAGMA quick_check").fetchone()[0]
    if integrity != "ok":
        errors.append(f"quick_check={integrity}")
    invalid_confirmed = database.execute(
        """
        SELECT t.id FROM tasks t
        WHERE t.status='CONFIRMED' AND NOT EXISTS (
          SELECT 1 FROM task_history h
          WHERE h.task_id=t.id AND h.to_status='CONFIRMED' AND h.from_status='SUCCEEDED'
        )
        """
    ).fetchall()
    if invalid_confirmed:
        errors.append("CONFIRMED 缺少 SUCCEEDED 转入历史: " + ",".join(row[0] for row in invalid_confirmed))
    active = database.execute("SELECT count(*) FROM executions WHERE status='RUNNING'").fetchone()[0]
    maximum = int(setting(database, "max_parallel_tasks", 6))
    if active > maximum:
        errors.append(f"active_executions={active} exceeds max_parallel_tasks={maximum}")
    orphan_running = database.execute(
        """
        SELECT t.id FROM tasks t
        WHERE t.status='RUNNING' AND NOT EXISTS (
          SELECT 1 FROM executions e WHERE e.task_id=t.id AND e.status='RUNNING'
        )
        """
    ).fetchall()
    if orphan_running:
        errors.append("RUNNING 任务缺少活动 execution: " + ",".join(row[0] for row in orphan_running))
    mismatched_locks = database.execute(
        """
        SELECT l.scope_key FROM scope_locks l
        LEFT JOIN executions e ON e.execution_id=l.execution_id
        WHERE e.execution_id IS NULL OR e.status<>'RUNNING' OR e.task_id<>l.task_id
        """
    ).fetchall()
    if mismatched_locks:
        errors.append("scope 锁与活动 execution 不一致: " + ",".join(row[0] for row in mismatched_locks))
    return {
        "ok": not errors,
        "schema_version": schema_version,
        "tasks": database.execute("SELECT count(*) FROM tasks").fetchone()[0],
        "active_executions": active,
        "max_parallel_tasks": maximum,
        "errors": errors,
    }


def record_health_event(
    database: sqlite3.Connection,
    component: str,
    status: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    database.execute(
        "INSERT INTO health_events(at, component, status, message, details_json) VALUES(?, ?, ?, ?, ?)",
        (now_shanghai(), component, status, message, json_dump(details or {})),
    )
