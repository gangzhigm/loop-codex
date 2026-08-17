"""任务持久化、依赖图检查和 Dashboard 任务投影。

本模块管理生命周期从属于任务本身的记录：任务定义、有序子记录、依赖、scope、附件、历史，
以及返回给调用方的标准 JSON 投影。execution 领取和恢复属于事务状态机，因此仍保留在
``control`` 模块中。

代码刻意保持显式。每个子表写入和兼容分支都直接可见，人工检查数据库时无需继续追踪通用
ORM 或仓储抽象。
"""

from __future__ import annotations

# 中文排查：任务主表、依赖、scope、附件、历史及 API 投影的持久化逻辑集中在这里。
# 写入异常按主字段校验、执行路由、子表替换、依赖环检测和事务提交顺序定位。
# task_dict 是 Dashboard 的统一投影出口；字段显示缺失时先确认数据库列，再检查兼容分支。

import re
import sqlite3
from typing import Any, Iterable

from loop_agent.configuration import (
    load_initialization_config,
    normalize_execution_target,
    resolve_execution_profile,
)
from loop_agent.constants import (
    CAPABILITY_LEVELS,
    EXECUTION_POLICIES,
    LOCK_MODES,
    PREFLIGHT_STATUSES,
    PRIORITIES,
    SCHEMA_USER_VERSION,
)
from loop_agent.database.compatibility import LEGACY_PROFILE_TO_CAPABILITY
from loop_agent.errors import LoopError
from loop_agent.serialization import json_dump, json_load, now_shanghai
from loop_agent.tasks.normalization import (
    load_result_diagnostic,
    normalize_result_diagnostic,
    normalize_split_suggestions,
    normalize_string_list,
)
from loop_agent.tasks.scopes import (
    normalize_scope,
    scope_conflicts_for_keys,
    scope_keys_conflict,
)


def _dependencies_ready_for_projection(
    database: sqlite3.Connection, task_id: str
) -> bool:
    """投影依赖就绪状态，但不改变队列状态。"""
    return (
        database.execute(
            "SELECT 1 FROM task_dependencies d JOIN tasks t ON t.id=d.dependency_id "
            "WHERE d.task_id=? AND t.status NOT IN ('SUCCEEDED','CONFIRMED') LIMIT 1",
            (task_id,),
        ).fetchone()
        is None
    )


def scope_queue_position(
    database: sqlite3.Connection,
    task_id: str,
    scope_keys: Iterable[str],
) -> int | None:
    """返回任务在待执行冲突 scope 中从 1 开始的队列位置。"""
    target_keys = list(scope_keys)
    if not target_keys or not _dependencies_ready_for_projection(database, task_id):
        return None
    candidates = database.execute(
        "SELECT id FROM tasks WHERE status='PENDING' AND preflight_status='READY' ORDER BY "
        "CASE priority WHEN 'blocker' THEN 0 WHEN 'critical' THEN 1 WHEN 'high' THEN 2 "
        "WHEN 'medium' THEN 3 ELSE 4 END, created_at, id"
    ).fetchall()
    position = 0
    for candidate in candidates:
        candidate_id = candidate["id"]
        if not _dependencies_ready_for_projection(database, candidate_id):
            continue
        candidate_keys = task_children(
            database, candidate_id, "task_scopes", "scope_key"
        )
        if not any(
            scope_keys_conflict(left, right)
            for left in target_keys
            for right in candidate_keys
        ):
            continue
        position += 1
        if candidate_id == task_id:
            return position
    return None


def replace_ordered_text(
    database: sqlite3.Connection,
    table: str,
    task_id: str,
    values: Iterable[str],
) -> None:
    """在调用方事务内替换一个任务的有序文本子记录。"""
    database.execute(f"DELETE FROM {table} WHERE task_id=?", (task_id,))
    database.executemany(
        f"INSERT INTO {table}(task_id, ordinal, text) VALUES(?, ?, ?)",
        [
            (task_id, index, str(value))
            for index, value in enumerate(values)
        ],
    )


def task_exists(database: sqlite3.Connection, task_id: str) -> bool:
    return (
        database.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone()
        is not None
    )


def dependency_cycle_path(
    database: sqlite3.Connection,
    replacement_task_id: str | None = None,
    replacement_dependencies: Iterable[str] | None = None,
) -> list[str] | None:
    """返回一条具体依赖环，并在末尾重复起始节点。"""
    graph: dict[str, list[str]] = {
        row[0]: []
        for row in database.execute("SELECT id FROM tasks ORDER BY id").fetchall()
    }
    for row in database.execute(
        "SELECT task_id, dependency_id FROM task_dependencies ORDER BY task_id, dependency_id"
    ).fetchall():
        graph.setdefault(row["task_id"], []).append(row["dependency_id"])
    if replacement_task_id is not None:
        graph[replacement_task_id] = list(replacement_dependencies or [])

    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def visit(task_id: str) -> list[str] | None:
        visiting.add(task_id)
        stack.append(task_id)
        for dependency in graph.get(task_id, []):
            if dependency in visiting:
                start = stack.index(dependency)
                return stack[start:] + [dependency]
            if dependency not in visited:
                cycle = visit(dependency)
                if cycle:
                    return cycle
        stack.pop()
        visiting.remove(task_id)
        visited.add(task_id)
        return None

    for task_id in sorted(graph):
        if task_id not in visited:
            cycle = visit(task_id)
            if cycle:
                return cycle
    return None


def set_task_dependencies(
    database: sqlite3.Connection,
    task_id: str,
    dependencies: Iterable[str],
) -> None:
    """检查依赖存在且全局无环后，替换任务依赖边。"""
    values = [str(dependency) for dependency in dependencies]
    if len(values) != len(set(values)):
        raise LoopError("任务依赖不能重复")
    if task_id in values:
        raise LoopError(f"任务不能依赖自身: {task_id} -> {task_id}")
    missing = [
        dependency
        for dependency in values
        if not task_exists(database, dependency)
    ]
    if missing:
        raise LoopError("依赖任务不存在: " + ", ".join(missing))
    cycle = dependency_cycle_path(database, task_id, values)
    if cycle:
        raise LoopError("循环依赖: " + " -> ".join(cycle))
    database.execute("DELETE FROM task_dependencies WHERE task_id=?", (task_id,))
    database.executemany(
        "INSERT INTO task_dependencies(task_id, dependency_id) VALUES(?, ?)",
        [(task_id, dependency) for dependency in values],
    )


def insert_task(
    database: sqlite3.Connection,
    task: dict[str, Any],
    actor: str = "migration",
    project_paths: Iterable[str] | None = None,
) -> None:
    """校验并插入一个任务及其全部从属子记录。

    事务由调用方管理。写入只支持当前 Schema，并强制执行 Planner 就绪约束。
    """
    task_id = task.get("id")
    if not isinstance(task_id, str) or not re.fullmatch(
        r"[A-Z][A-Z0-9_-]*", task_id
    ):
        raise LoopError(f"任务 id 无效: {task_id}")
    if task_exists(database, task_id):
        raise LoopError(f"任务 id 已存在: {task_id}")
    status = task.get("status", "PENDING")
    priority = task.get("priority", "medium")
    if "execution_profile" in task:
        raise LoopError("当前任务输入不支持 execution_profile，请使用 capability_level")
    capability_level = task.get("capability_level")
    estimated_capability_level = task.get("estimated_capability_level")
    execution_policy = task.get("execution_policy")
    current_schema = int(database.execute("PRAGMA user_version").fetchone()[0])
    if current_schema != SCHEMA_USER_VERSION:
        raise LoopError("当前任务写入要求先把数据库迁移到最新 Schema")
    config = load_initialization_config()
    runtime_environment = task.get("runtime_environment")
    if runtime_environment is None:
        runtime_environment = config["planner"]["default_runtime_environment"]
    provider_id = task.get("provider_id")
    if (
        provider_id is None
        and runtime_environment == config["planner"]["default_runtime_environment"]
    ):
        provider_id = config["planner"]["provider_id"]
    valid_statuses = {
        "DRAFT",
        "NEEDS_REVIEW",
        "PENDING",
        "RUNNING",
        "WAITING_CONFLICT",
        "WAITING_HUMAN",
        "SUCCEEDED",
        "CONFIRMED",
        "FAILED",
        "CANCELLED",
    }
    if status not in valid_statuses:
        raise LoopError(f"任务状态无效: {status}")
    if priority not in PRIORITIES:
        raise LoopError(f"任务优先级无效: {priority}")
    if estimated_capability_level is None:
        estimated_capability_level = capability_level
    if (
        status not in {"DRAFT", "NEEDS_REVIEW"}
        and capability_level is None
    ):
        capability_level = "L2"
    if estimated_capability_level is None:
        estimated_capability_level = capability_level
    if (
        estimated_capability_level is not None
        and estimated_capability_level not in CAPABILITY_LEVELS
    ):
        raise LoopError(
            f"任务预估能力等级无效: {estimated_capability_level}"
        )
    if capability_level is not None and capability_level not in CAPABILITY_LEVELS:
        raise LoopError(f"任务能力等级无效: {capability_level}")
    if execution_policy is None:
        execution_policy = "automatic"
    if execution_policy not in EXECUTION_POLICIES:
        raise LoopError(f"执行策略无效: {execution_policy}")
    runtime_environment, provider_id = normalize_execution_target(
        runtime_environment, provider_id
    )
    if capability_level is not None:
        resolve_execution_profile(
            runtime_environment, provider_id, capability_level
        )
    stamp = task.get("created_at") or now_shanghai()
    progress = task.get("progress") or {}
    result = task.get("result") or {}
    diagnostic = normalize_result_diagnostic(result.get("diagnostic"))
    diagnostic_json = json_dump(diagnostic) if diagnostic is not None else None
    human = task.get("human_intervention") or {}
    common_tail = (
        task.get("assigned_agent"),
        stamp,
        task.get("started_at"),
        task.get("updated_at") or stamp,
        task.get("heartbeat_at"),
        task.get("completed_at"),
        task.get("archived_at"),
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
        int(task.get("row_version", 1)),
    )
    if status == "DRAFT":
        preflight_status = task.get("preflight_status", "UNINSPECTED")
    elif status == "NEEDS_REVIEW":
        preflight_status = task.get("preflight_status", "FAILED")
    else:
        preflight_status = task.get("preflight_status", "READY")
    if preflight_status not in PREFLIGHT_STATUSES:
        raise LoopError(f"preflight_status 无效: {preflight_status}")
    if status == "DRAFT" and preflight_status not in {
        "UNINSPECTED",
        "INSPECTING",
    }:
        raise LoopError("DRAFT 只能处于 UNINSPECTED 或 INSPECTING")
    if status == "NEEDS_REVIEW" and preflight_status != "FAILED":
        raise LoopError("NEEDS_REVIEW 必须处于 FAILED preflight")
    if status not in {"DRAFT", "NEEDS_REVIEW"} and preflight_status != "READY":
        raise LoopError(f"{status} 任务必须处于 READY preflight")
    scope_input = normalize_string_list(task.get("scope") or [], "scope")
    scope_hint = normalize_string_list(
        task.get("scope_hint", scope_input), "scope_hint"
    )
    exact_scopes = scope_input if preflight_status == "READY" else []
    technical_acceptance = normalize_string_list(
        task.get("technical_acceptance")
        or (task.get("acceptance") if preflight_status == "READY" else [])
        or [],
        "technical_acceptance",
    )
    evidence = normalize_string_list(
        task.get("preflight_evidence")
        or (["任务由当前受控写入路径建立。"] if preflight_status == "READY" else []),
        "preflight_evidence",
    )
    lock_mode = task.get(
        "lock_mode", "project" if preflight_status == "READY" else None
    )
    if lock_mode is not None and lock_mode not in LOCK_MODES:
        raise LoopError(f"lock_mode 无效: {lock_mode}")
    split_suggestions = normalize_split_suggestions(
        task.get("split_suggestions")
    )
    if preflight_status == "READY":
        if capability_level is None or not exact_scopes or lock_mode is None:
            raise LoopError(
                "READY 任务必须具备最终 capability_level、scope 和 lock_mode"
            )
        if not technical_acceptance or not evidence:
            raise LoopError("READY 任务必须具备技术验收补充和检查证据")
        if split_suggestions:
            raise LoopError("READY 任务不得保留拆分建议")
    elif status in {"DRAFT", "NEEDS_REVIEW"}:
        capability_level = None
        lock_mode = None
    columns = (
        "id, title, description, status, priority, estimated_capability_level, capability_level, "
        "runtime_environment, provider_id, execution_policy, preflight_status, preflight_execution_id, "
        "preflight_started_at, preflight_completed_at, preflight_failure, scope_hint_json, lock_mode, "
        "split_suggestions_json, assigned_agent, created_at, started_at, updated_at, heartbeat_at, "
        "completed_at, archived_at, attempt, progress_percent, progress_summary, progress_next_step, "
        "result_summary, result_error, result_diagnostic_json, human_required, human_question, "
        "human_options_json, human_requested_at, human_responded_at, human_response, row_version"
    )
    values = (
        task_id,
        str(task.get("title") or task_id),
        str(task.get("description") or ""),
        status,
        priority,
        estimated_capability_level,
        capability_level,
        runtime_environment,
        provider_id,
        execution_policy,
        preflight_status,
        task.get("preflight_execution_id"),
        task.get("preflight_started_at"),
        task.get("preflight_completed_at"),
        task.get("preflight_failure"),
        json_dump(scope_hint),
        lock_mode,
        json_dump(split_suggestions),
        *common_tail[:13],
        diagnostic_json,
        *common_tail[13:],
    )
    placeholders = ", ".join("?" for _ in values)
    database.execute(
        f"INSERT INTO tasks({columns}) VALUES({placeholders})", values
    )

    set_task_dependencies(database, task_id, task.get("depends_on") or [])
    scopes_to_store = exact_scopes
    stored_lock_mode = lock_mode
    normalized_scopes = [
        normalize_scope(scope, stored_lock_mode, project_paths)
        for scope in scopes_to_store
    ]
    normalized_keys = [item["scope_key"] for item in normalized_scopes]
    if (
        len(normalized_keys) != len(set(normalized_keys))
        and stored_lock_mode != "project"
    ):
        raise LoopError("scope 规范化后不能重复")
    for index, item in enumerate(normalized_scopes):
        database.execute(
            "INSERT INTO task_scopes(task_id, ordinal, scope, scope_key) VALUES(?, ?, ?, ?)",
            (task_id, index, item["scope"], item["scope_key"]),
        )
    replace_ordered_text(
        database, "task_acceptance", task_id, task.get("acceptance") or []
    )
    replace_ordered_text(
        database,
        "task_technical_acceptance",
        task_id,
        technical_acceptance,
    )
    replace_ordered_text(
        database, "task_preflight_evidence", task_id, evidence
    )
    replace_ordered_text(
        database,
        "task_completed_items",
        task_id,
        progress.get("completed") or [],
    )
    replace_ordered_text(
        database,
        "task_verifications",
        task_id,
        result.get("verification") or [],
    )
    for index, attachment in enumerate(task.get("attachments") or []):
        database.execute(
            "INSERT INTO task_attachments(task_id, ordinal, path, sha256, role, saved_at) VALUES(?, ?, ?, ?, ?, ?)",
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
        {
            "at": stamp,
            "from": None,
            "to": status,
            "actor": actor,
            "reason": "任务已创建。",
        }
    ]
    for entry in history:
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) VALUES(?, ?, ?, ?, ?, ?)",
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
    """读取任务某个有序子表字段。"""
    rows = database.execute(
        f"SELECT {column} FROM {table} WHERE task_id=? ORDER BY {order_column}",
        (task_id,),
    ).fetchall()
    return [row[0] for row in rows]


def task_dict(
    database: sqlite3.Connection, row: sqlite3.Row
) -> dict[str, Any]:
    """构建单条任务记录的标准 API 表示。"""
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
            "SELECT at, from_status, to_status, actor, reason FROM task_history WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()
    ]
    conflicts = [
        dict(item)
        for item in database.execute(
            "SELECT scope_key, blocker_task_id, blocker_execution_id, detected_at FROM task_conflicts WHERE task_id=? ORDER BY scope_key",
            (task_id,),
        ).fetchall()
    ]
    columns = set(row.keys())
    if "capability_level" in columns:
        capability_level = row["capability_level"]
        execution_policy = row["execution_policy"]
        provider_id = row["provider_id"]
        runtime_environment = row["runtime_environment"]
    else:
        execution_profile = row["execution_profile"]
        capability_level = LEGACY_PROFILE_TO_CAPABILITY[execution_profile]
        execution_policy = (
            "manual" if execution_profile == "exceptional" else "automatic"
        )
        legacy_environment = row["runtime_environment"]
        runtime_environment = (
            "self_hosted_agent"
            if legacy_environment == "deepseek"
            else legacy_environment
        )
        provider_id = "deepseek" if legacy_environment == "deepseek" else None
    scopes = task_children(database, task_id, "task_scopes", "scope")
    scope_keys = task_children(database, task_id, "task_scopes", "scope_key")
    acceptance = task_children(database, task_id, "task_acceptance")
    dependencies = task_children(
        database,
        task_id,
        "task_dependencies",
        "dependency_id",
        "dependency_id",
    )
    if "preflight_status" in columns:
        scope_hint = normalize_string_list(
            json_load(row["scope_hint_json"], []), "scope_hint"
        )
        split_suggestions = normalize_split_suggestions(
            json_load(row["split_suggestions_json"], [])
        )
        technical_acceptance = task_children(
            database, task_id, "task_technical_acceptance"
        )
        evidence = task_children(
            database, task_id, "task_preflight_evidence"
        )
        preflight_status = row["preflight_status"]
        estimated_capability_level = row["estimated_capability_level"]
        lock_mode = row["lock_mode"]
        preflight_execution_id = row["preflight_execution_id"]
        preflight_started_at = row["preflight_started_at"]
        preflight_completed_at = row["preflight_completed_at"]
        preflight_failure = row["preflight_failure"]
    else:
        scope_hint = scopes
        split_suggestions = []
        technical_acceptance = acceptance
        evidence = []
        preflight_status = "READY"
        estimated_capability_level = capability_level
        lock_mode = "project"
        preflight_execution_id = None
        preflight_started_at = None
        preflight_completed_at = row["updated_at"]
        preflight_failure = None
    operator_definition = {
        "description": row["description"],
        "acceptance": acceptance,
        "priority": row["priority"],
        "runtime_environment": runtime_environment,
        "provider_id": provider_id,
        "execution_policy": execution_policy,
        "depends_on": dependencies,
        "attachments": attachments,
        "scope_hint": scope_hint,
        "estimated_capability_level": estimated_capability_level,
    }
    planner_supplement = {
        "preflight_status": preflight_status,
        "execution_id": preflight_execution_id,
        "started_at": preflight_started_at,
        "completed_at": preflight_completed_at,
        "failure": preflight_failure,
        "capability_level": capability_level,
        "scope": scopes,
        "lock_mode": lock_mode,
        "technical_acceptance": technical_acceptance,
        "evidence": evidence,
        "split_suggestions": split_suggestions,
    }
    blocking_scopes = (
        scope_conflicts_for_keys(database, scope_keys, exclude_task_id=task_id)
        if row["status"] == "PENDING"
        else []
    )
    blocked_by_task_ids = sorted(
        {item["blocker_task_id"] for item in blocking_scopes}
    )
    blocked_scope_keys = sorted(
        {item["requested_scope_key"] for item in blocking_scopes}
    )
    blocked_key_set = set(blocked_scope_keys)
    blocked_scopes = sorted(
        {
            scope
            for scope, scope_key in zip(scopes, scope_keys)
            if scope_key in blocked_key_set
        }
    )
    queue_position = (
        scope_queue_position(database, task_id, scope_keys)
        if row["status"] == "PENDING"
        else None
    )
    return {
        "id": task_id,
        "title": row["title"],
        "description": row["description"],
        "status": row["status"],
        "priority": row["priority"],
        "preflight_status": preflight_status,
        "estimated_capability_level": estimated_capability_level,
        "capability_level": capability_level,
        "execution_policy": execution_policy,
        "provider_id": provider_id,
        "runtime_environment": runtime_environment,
        "assigned_agent": row["assigned_agent"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "updated_at": row["updated_at"],
        "heartbeat_at": row["heartbeat_at"],
        "completed_at": row["completed_at"],
        "archived_at": row["archived_at"],
        "attempt": row["attempt"],
        "depends_on": dependencies,
        "scope_hint": scope_hint,
        "scope": scopes,
        "scope_keys": scope_keys,
        "lock_mode": lock_mode,
        "blocked_by_task_ids": blocked_by_task_ids,
        "blocked_scopes": blocked_scopes,
        "blocked_scope_keys": blocked_scope_keys,
        "blocking_scopes": blocking_scopes,
        "scope_queue_position": queue_position,
        "acceptance": acceptance,
        "technical_acceptance": technical_acceptance,
        "preflight_evidence": evidence,
        "split_suggestions": split_suggestions,
        "operator_definition": operator_definition,
        "planner_supplement": planner_supplement,
        "progress": {
            "percent": row["progress_percent"],
            "summary": row["progress_summary"],
            "completed": task_children(
                database, task_id, "task_completed_items"
            ),
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
            "verification": task_children(
                database, task_id, "task_verifications"
            ),
            "error": row["result_error"],
            "diagnostic": (
                load_result_diagnostic(row["result_diagnostic_json"])
                if "result_diagnostic_json" in columns
                and row["result_diagnostic_json"] is not None
                else None
            ),
        },
        "history": history,
        "conflicts": conflicts,
        "row_version": row["row_version"],
    }


def all_tasks(database: sqlite3.Connection) -> list[dict[str, Any]]:
    """按稳定的队列展示顺序返回全部任务。"""
    rows = database.execute(
        "SELECT * FROM tasks ORDER BY CASE priority WHEN 'blocker' THEN 0 WHEN 'critical' THEN 1 "
        "WHEN 'high' THEN 2 WHEN 'medium' THEN 3 ELSE 4 END, created_at, id"
    ).fetchall()
    return [task_dict(database, row) for row in rows]
