from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from loopdb import (
    ARCHIVABLE_STATUSES,
    BASE_DIR,
    CAPABILITY_LEVELS,
    CLAIM_RUNTIME_ENVIRONMENTS,
    CONFIG_PATH,
    DEFAULT_DB,
    DEPENDENCY_COMPLETE_STATUSES,
    EXECUTION_PROFILES,
    FINAL_EXECUTION_STATUSES,
    PRIORITIES,
    RUNTIME_ENVIRONMENTS,
    LoopError,
    all_tasks,
    bump_revision,
    commit,
    connect,
    configured_projects,
    execution_setting,
    expires_at,
    global_parallel_limit,
    initialize_schema,
    insert_task,
    json_dump,
    load_initialization_config,
    migrate_schema,
    now_shanghai,
    parse_project_registry,
    platform_parallel_limit,
    replace_ordered_text,
    legacy_profile_for,
    LEGACY_PROFILE_TO_CAPABILITY,
    LOCK_MODES,
    normalize_execution_target,
    normalize_result_diagnostic,
    normalize_scope,
    normalize_split_suggestions,
    normalize_string_list,
    resolve_execution_profile,
    resolve_scope_key,
    rollback,
    scope_conflicts_for_keys,
    set_task_dependencies,
    state_payload,
    task_dict,
    task_exists,
    transaction,
    uses_capability_schema,
    uses_recovery_schema,
    uses_result_diagnostic_schema,
    uses_preflight_schema,
    validate_database,
)


LEGACY_STATUS_MAP = {
    "CLAIMED": "WAITING_HUMAN",
    "BLOCKED": "WAITING_HUMAN",
    "STALLED": "WAITING_HUMAN",
}

PLANNER_ESCALATION_MARKERS = {
    "L5": "APPROVED_PLANNER_ESCALATION: L5",
    "manual": "APPROVED_PLANNER_ESCALATION: manual",
}

SUSPICIOUS_QUESTION_MARK_RUN = re.compile(r"\?{4,}")


def output(payload: dict[str, Any], exit_code: int = 0) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if exit_code:
        raise SystemExit(exit_code)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_json_source(source: str) -> Any:
    if source == "-":
        return json.loads(sys.stdin.read())
    return read_json(Path(source).resolve())


def read_preflight_report(source: str) -> Any:
    if source != "-":
        raise LoopError("Planner 预检结果只允许通过 UTF-8 stdin 提交")
    return read_json_source(source)


def validate_preflight_text_integrity(value: Any, field: str = "payload") -> None:
    """Reject unmistakable UTF-8 writeback corruption before Planner data reaches SQLite."""
    if isinstance(value, str):
        if "\ufffd" in value or SUSPICIOUS_QUESTION_MARK_RUN.search(value):
            raise LoopError(f"Planner UTF-8 写回文本损坏: {field}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            validate_preflight_text_integrity(item, f"{field}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            validate_preflight_text_integrity(item, f"{field}.{key}")


def require_expected_row_version(args: argparse.Namespace, actual: int) -> None:
    expected = getattr(args, "expected_row_version", None)
    if expected is not None and expected != actual:
        raise LoopError(f"任务已发生并发变化：expected row_version={expected}, actual={actual}")


def backup_legacy(tasks_path: Path, inbox_path: Path, backup_root: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    destination = backup_root / f"sqlite-migration-{stamp}"
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copy2(tasks_path, destination / tasks_path.name)
    shutil.copy2(inbox_path, destination / inbox_path.name)
    return destination


def normalize_legacy_task(task: dict[str, Any]) -> dict[str, Any]:
    normalized = json.loads(json.dumps(task, ensure_ascii=False))
    normalized.setdefault("runtime_environment", "codex_automation")
    old_status = normalized.get("status", "PENDING")
    mapped = LEGACY_STATUS_MAP.get(old_status, old_status)
    if old_status == "RUNNING":
        mapped = "WAITING_HUMAN"
    if mapped != old_status:
        stamp = now_shanghai()
        normalized["status"] = mapped
        normalized["assigned_agent"] = None
        normalized["heartbeat_at"] = None
        normalized["updated_at"] = stamp
        normalized.setdefault("progress", {})["next_step"] = "迁移后需要人工决定是否继续。"
        normalized["human_intervention"] = {
            "required": True,
            "question": "旧执行状态在 SQLite 迁移时无法安全续接，是否重新排队？",
            "options": ["重新排队", "保持等待"],
            "requested_at": stamp,
            "responded_at": None,
            "response": None,
        }
        normalized.setdefault("history", []).append(
            {
                "at": stamp,
                "from": old_status,
                "to": mapped,
                "actor": "sqlite-migration",
                "reason": "旧活动或兼容状态在迁移时转换为等待人工。",
            }
        )
    if normalized.get("status") == "CONFIRMED" and not normalized.get("archived_at"):
        normalized["archived_at"] = (
            normalized.get("completed_at") or normalized.get("updated_at") or normalized.get("created_at")
        )
    return normalized


def command_migrate_legacy(args: argparse.Namespace) -> None:
    database_path = Path(args.db).resolve()
    tasks_path = Path(args.tasks).resolve()
    inbox_path = Path(args.inbox).resolve()
    registry_path = Path(args.registry).resolve()
    initialization_config = load_initialization_config(args.config)
    if not tasks_path.exists() or not inbox_path.exists() or not registry_path.exists():
        raise LoopError("迁移输入文件不存在")
    if database_path.exists() and database_path.stat().st_size > 0 and not args.force:
        probe = connect(database_path)
        try:
            initialize_schema(probe)
            count = probe.execute("SELECT count(*) FROM tasks").fetchone()[0]
            if count:
                raise LoopError(f"数据库已有 {count} 个任务；拒绝重复迁移")
        finally:
            probe.close()

    backup_path = backup_legacy(tasks_path, inbox_path, Path(args.backup_dir).resolve())
    legacy = read_json(tasks_path)
    inbox = read_json(inbox_path)
    database = connect(database_path)
    try:
        initialize_schema(database)
        transaction(database)
        database.execute("PRAGMA defer_foreign_keys = ON")
        projects = parse_project_registry(registry_path)
        project_paths = [item["path"] for item in projects]
        for task in legacy.get("tasks") or []:
            insert_task(
                database,
                normalize_legacy_task(task),
                actor="sqlite-migration",
                project_paths=project_paths,
            )
        for item in inbox.get("tasks") or []:
            stamp = now_shanghai()
            task = {
                **item,
                "runtime_environment": item.get("runtime_environment", "codex_automation"),
                "status": item.get("status", "PENDING"),
                "created_at": stamp,
                "updated_at": stamp,
                "progress": {
                    "percent": 0,
                    "summary": "任务从旧 INBOX 迁移。",
                    "completed": [],
                    "next_step": "等待并发 Worker 领取。",
                },
                "result": {"summary": None, "verification": [], "error": None},
                "history": [
                    {
                        "at": stamp,
                        "from": None,
                        "to": item.get("status", "PENDING"),
                        "actor": "sqlite-migration",
                        "reason": "从旧 INBOX.json 迁移。",
                    }
                ],
            }
            insert_task(
                database,
                task,
                actor="sqlite-migration",
                project_paths=project_paths,
            )
        validation = validate_database(database)
        expected = len(legacy.get("tasks") or []) + len(inbox.get("tasks") or [])
        if not validation["ok"] or validation["tasks"] != expected:
            raise LoopError(f"迁移校验失败: expected={expected}, validation={validation}")
        commit(database)
        output(
            {
                "outcome": "LEGACY_MIGRATED",
                "database": str(database_path),
                "backup": str(backup_path),
                "tasks": expected,
                "projects": len(projects),
                "missing_projects": [item["path"] for item in projects if not item["exists_on_disk"]],
                "validation": validation,
                "initialization_config": initialization_config,
            }
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_migrate(args: argparse.Namespace) -> None:
    database = connect(args.db)
    try:
        result = migrate_schema(database)
        validation = validate_database(database)
        if not validation["ok"]:
            raise LoopError(f"迁移后校验失败: {validation}")
        output({"outcome": "MIGRATED" if result["migrated"] else "ALREADY_CURRENT", **result})
    finally:
        database.close()


def stalled_executions(database: sqlite3.Connection) -> list[dict[str, Any]]:
    stamp = now_shanghai()
    current = datetime.fromisoformat(stamp)
    stalled_cutoff = (
        current
        - timedelta(seconds=int(execution_setting("stalled_after_seconds", 300)))
    ).isoformat(timespec="milliseconds")
    capability_schema = uses_capability_schema(database)
    recovery_schema = uses_recovery_schema(database)
    if capability_schema:
        statuses = "('RUNNING','STALLED','TIMED_OUT')" if recovery_schema else "('RUNNING')"
        rows = database.execute(
            "SELECT execution_id, task_id, status, started_at, heartbeat_at, lease_expires_at, "
            "runtime_environment, provider_id, capability_level, execution_policy, "
            "attempt_timeout_seconds, "
            + ("termination_reason, recovery_required " if recovery_schema else "NULL AS termination_reason, 0 AS recovery_required ")
            + f"FROM executions WHERE status IN {statuses}"
        ).fetchall()
    else:
        rows = database.execute(
            "SELECT e.execution_id, e.task_id, e.status, e.started_at, e.heartbeat_at, e.lease_expires_at, "
            "t.runtime_environment, NULL AS provider_id, NULL AS capability_level, NULL AS execution_policy, "
            "NULL AS attempt_timeout_seconds, NULL AS termination_reason, 0 AS recovery_required FROM executions e "
            "JOIN tasks t ON t.id=e.task_id WHERE e.status='RUNNING'"
        ).fetchall()
    stalled: list[dict[str, Any]] = []
    for execution in rows:
        heartbeat_stalled = execution["heartbeat_at"] <= stalled_cutoff
        lease_expired = execution["lease_expires_at"] <= stamp
        attempt_timed_out = False
        if execution["attempt_timeout_seconds"] is not None:
            attempt_timed_out = (
                datetime.fromisoformat(execution["started_at"])
                + timedelta(seconds=int(execution["attempt_timeout_seconds"]))
                <= current
            )
        if not (heartbeat_stalled or lease_expired or attempt_timed_out or execution["recovery_required"]):
            continue
        runtime_environment = execution["runtime_environment"]
        if runtime_environment == "deepseek":
            runtime_environment = "self_hosted_agent"
        stalled.append(
            {
                "execution_id": execution["execution_id"],
                "task_id": execution["task_id"],
                "execution_status": execution["status"],
                "runtime_environment": runtime_environment,
                "provider_id": execution["provider_id"],
                "capability_level": execution["capability_level"],
                "execution_policy": execution["execution_policy"],
                "heartbeat_stalled": heartbeat_stalled,
                "lease_expired": lease_expired,
                "attempt_timed_out": attempt_timed_out,
                "termination_reason": execution["termination_reason"],
                "recovery_confirmation": (
                    "human_confirmed_safe"
                    if runtime_environment == "codex_automation"
                    else "runner_confirmed_terminated"
                ),
            }
        )
    return stalled


def _recovery_reason(recovery: dict[str, Any]) -> tuple[str, str]:
    codes = []
    labels = []
    if recovery["heartbeat_stalled"]:
        codes.append("HEARTBEAT_STALLED")
        labels.append("心跳停滞")
    if recovery["lease_expired"]:
        codes.append("LEASE_EXPIRED")
        labels.append("租约过期")
    if recovery["attempt_timed_out"]:
        codes.append("ATTEMPT_TIMED_OUT")
        labels.append("单次 attempt 超时")
    return "+".join(codes), "、".join(labels)


def transition_recovery_states(
    database: sqlite3.Connection, recoveries: list[dict[str, Any]]
) -> None:
    if not uses_recovery_schema(database):
        return
    stamp = now_shanghai()
    for recovery in recoveries:
        old_status = recovery["execution_status"]
        if old_status == "TIMED_OUT" or (old_status == "STALLED" and not recovery["attempt_timed_out"]):
            continue
        new_status = "TIMED_OUT" if recovery["attempt_timed_out"] else "STALLED"
        reason_code, reason_label = _recovery_reason(recovery)
        outcome = "INFRASTRUCTURE_TIMEOUT" if new_status == "TIMED_OUT" else "RECOVERY_REQUIRED"
        updated = database.execute(
            "UPDATE executions SET status=?, finished_at=?, outcome=?, termination_reason=?, "
            "recovery_required=1 WHERE execution_id=? AND status=?",
            (new_status, stamp, outcome, reason_code, recovery["execution_id"], old_status),
        )
        if not updated.rowcount:
            continue
        database.execute(
            "UPDATE scope_locks SET status='QUARANTINED', quarantined_at=COALESCE(quarantined_at, ?), "
            "quarantine_reason=? WHERE execution_id=?",
            (stamp, reason_label, recovery["execution_id"]),
        )
        if old_status == "STALLED":
            database.execute(
                "UPDATE tasks SET updated_at=?, progress_summary=?, row_version=row_version+1 "
                "WHERE id=? AND status='WAITING_HUMAN'",
                (stamp, f"execution 已到达 attempt timeout；scope 继续隔离。原因：{reason_label}。", recovery["task_id"]),
            )
            database.execute(
                "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
                "VALUES(?, ?, 'WAITING_HUMAN', 'WAITING_HUMAN', 'execution-timeout-detector', ?)",
                (recovery["task_id"], stamp, f"execution 转为 TIMED_OUT；scope 继续隔离。原因：{reason_label}。"),
            )
            continue
        is_codex = recovery["runtime_environment"] == "codex_automation"
        question = (
            f"execution {recovery['execution_id']} 已离开活动容量，但 scope 仍处于隔离状态；"
            + (
                "请确认旧 Codex 客户端会话已结束，再选择重新排队、标记失败或继续等待。"
                if is_codex
                else "受控 Runner 确认旧进程树已终止后，可重新排队、标记失败或继续等待。"
            )
        )
        task_updated = database.execute(
            "UPDATE tasks SET status='WAITING_HUMAN', human_required=1, human_question=?, "
            "human_options_json=?, human_requested_at=?, updated_at=?, completed_at=NULL, "
            "progress_summary=?, progress_next_step='等待安全确认与恢复处置。', row_version=row_version+1 "
            "WHERE id=? AND status='RUNNING' AND assigned_agent=?",
            (
                question,
                json.dumps(["确认结束并重新排队", "确认结束并标记失败", "继续等待"], ensure_ascii=False),
                stamp,
                stamp,
                f"execution 已转为 {new_status} 并释放活动容量；scope 保持隔离。原因：{reason_label}。",
                recovery["task_id"],
                recovery["execution_id"],
            ),
        )
        if task_updated.rowcount:
            database.execute(
                "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
                "VALUES(?, ?, 'RUNNING', 'WAITING_HUMAN', 'execution-liveness-detector', ?)",
                (
                    recovery["task_id"], stamp,
                    f"execution 转为 {new_status} 并释放活动容量；scope 转为 QUARANTINED。原因：{reason_label}。",
                ),
            )


def recovery_required_records(database: sqlite3.Connection) -> list[dict[str, Any]]:
    if not uses_recovery_schema(database):
        return stalled_executions(database)
    rows = database.execute(
        "SELECT e.execution_id, e.task_id, e.status, e.runtime_environment, e.provider_id, "
        "e.capability_level, e.execution_policy, e.termination_reason, e.recovery_action, "
        "l.scope_key, l.status AS scope_status, l.quarantined_at, l.quarantine_reason "
        "FROM executions e JOIN scope_locks l ON l.execution_id=e.execution_id "
        "WHERE e.recovery_required=1 AND l.status='QUARANTINED' "
        "ORDER BY e.started_at, l.scope_key"
    ).fetchall()
    records: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = records.setdefault(
            row["execution_id"],
            {
                "execution_id": row["execution_id"],
                "task_id": row["task_id"],
                "execution_status": row["status"],
                "runtime_environment": row["runtime_environment"],
                "provider_id": row["provider_id"],
                "capability_level": row["capability_level"],
                "execution_policy": row["execution_policy"],
                "termination_reason": row["termination_reason"],
                "heartbeat_stalled": "HEARTBEAT_STALLED" in (row["termination_reason"] or ""),
                "lease_expired": "LEASE_EXPIRED" in (row["termination_reason"] or ""),
                "attempt_timed_out": "ATTEMPT_TIMED_OUT" in (row["termination_reason"] or ""),
                "scope_status": row["scope_status"],
                "quarantined_at": row["quarantined_at"],
                "quarantine_reason": row["quarantine_reason"],
                "scope_keys": [],
                "recovery_confirmation": (
                    "human_confirmed_safe"
                    if row["runtime_environment"] == "codex_automation"
                    else "runner_confirmed_terminated"
                ),
            },
        )
        item["scope_keys"].append(row["scope_key"])
    return list(records.values())


def command_recover(args: argparse.Namespace) -> None:
    database = connect(args.db)
    try:
        transaction(database)
        transition_recovery_states(database, stalled_executions(database))
        execution = database.execute(
            "SELECT * FROM executions WHERE execution_id=?", (args.execution_id,)
        ).fetchone()
        if not execution:
            raise LoopError("execution 不存在")
        if not execution["recovery_required"]:
            if execution["recovery_action"] and (args.action is None or execution["recovery_action"] == args.action):
                commit(database)
                output({
                    "outcome": "ALREADY_RECOVERED", "execution_id": args.execution_id,
                    "task_id": execution["task_id"], "recovery_action": execution["recovery_action"],
                })
                return
            raise LoopError("execution 不处于待恢复状态")
        candidates = {item["execution_id"]: item for item in recovery_required_records(database)}
        recovery = candidates.get(args.execution_id)
        if not recovery:
            raise LoopError("execution 缺少 QUARANTINED scope，数据库状态不一致")
        platform = recovery["runtime_environment"]
        if platform == "codex_automation":
            if not args.human_confirmed_safe or args.runner_confirmed_terminated:
                raise LoopError("Codex 客户端 execution 只能在人工确认旧会话不再修改后恢复")
            actor = "human-safe-recovery"
        else:
            if not args.runner_confirmed_terminated or args.human_confirmed_safe:
                raise LoopError("受控 Runner 平台必须确认旧进程已终止后恢复")
            actor = "runner-safe-recovery"
        task = database.execute(
            "SELECT status, attempt, assigned_agent, human_response, row_version FROM tasks WHERE id=?",
            (execution["task_id"],),
        ).fetchone()
        if not task or task["status"] != "WAITING_HUMAN" or task["assigned_agent"] != args.execution_id:
            raise LoopError("待恢复任务状态或 execution fencing 不匹配")
        require_expected_row_version(args, task["row_version"])
        stamp = now_shanghai()
        action = args.action
        if action is None:
            action = "requeue" if task["attempt"] < int(execution_setting("max_attempts", 2)) else "failed"
        if action == "wait":
            if execution["recovery_action"] == "wait" and task["human_response"] == "continue_waiting":
                commit(database)
                output({
                    "outcome": "ALREADY_WAITING", "execution_id": args.execution_id,
                    "task_id": execution["task_id"], "task_status": "WAITING_HUMAN",
                    "scope_status": "QUARANTINED",
                })
                return
            database.execute(
                "UPDATE executions SET recovery_action='wait' WHERE execution_id=?",
                (args.execution_id,),
            )
            database.execute(
                "UPDATE tasks SET human_responded_at=?, human_response='continue_waiting', updated_at=?, "
                "progress_next_step='继续等待旧会话结束确认；scope 保持隔离。', "
                "row_version=row_version+1 WHERE id=?",
                (stamp, stamp, execution["task_id"]),
            )
            database.execute(
                "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
                "VALUES(?, ?, 'WAITING_HUMAN', 'WAITING_HUMAN', ?, '选择继续等待；活动容量保持释放，scope 保持隔离。')",
                (execution["task_id"], stamp, actor),
            )
            revision = bump_revision(database, actor)
            commit(database)
            output({
                "outcome": "WAITING", "execution_id": args.execution_id,
                "task_id": execution["task_id"], "task_status": "WAITING_HUMAN",
                "scope_status": "QUARANTINED", "revision": revision,
            })
            return
        new_status = "PENDING" if action == "requeue" else "FAILED"
        reason = recovery["quarantine_reason"] or recovery["termination_reason"]
        summary = (
            f"{reason}；旧执行已确认结束，隔离已释放并重新排队。"
            if new_status == "PENDING"
            else f"{reason}；旧执行已确认结束，人工选择标记 FAILED。"
        )
        database.execute("DELETE FROM scope_locks WHERE execution_id=?", (args.execution_id,))
        database.execute(
            "UPDATE executions SET recovery_required=0, recovered_at=?, recovery_action=? WHERE execution_id=?",
            (stamp, action, args.execution_id),
        )
        diagnostic_reset = "result_diagnostic_json=NULL, " if uses_result_diagnostic_schema(database) else ""
        database.execute(
            f"UPDATE tasks SET status=?, assigned_agent=NULL, heartbeat_at=NULL, updated_at=?, "
            "completed_at=?, progress_percent=?, progress_summary=?, progress_next_step=?, result_error=?, "
            f"{diagnostic_reset}human_required=0, human_question=NULL, "
            "human_options_json='[]', human_requested_at=NULL, "
            "human_responded_at=?, human_response=?, row_version=row_version+1 WHERE id=?",
            (
                new_status, stamp, stamp if new_status == "FAILED" else None,
                100 if new_status == "FAILED" else 0, summary,
                "等待下一次兼容执行器领取。" if new_status == "PENDING" else None,
                summary if new_status == "FAILED" else None, stamp, action, execution["task_id"],
            ),
        )
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, 'WAITING_HUMAN', ?, ?, ?)",
            (execution["task_id"], stamp, new_status, actor, summary),
        )
        requeued: list[str] = []
        revision = bump_revision(database, actor)
        commit(database)
        output(
            {
                "outcome": "RECOVERED",
                "execution_id": args.execution_id,
                "task_id": execution["task_id"],
                "task_status": new_status,
                "runtime_environment": platform,
                "recovery_action": action,
                "requeued_conflicts": requeued,
                "revision": revision,
            }
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


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


def recover_timed_out_preflights(database: sqlite3.Connection) -> list[str]:
    if not uses_preflight_schema(database):
        return []
    settings = load_initialization_config()["planner"]
    stamp = now_shanghai()
    current = datetime.fromisoformat(stamp)
    stalled_cutoff = current - timedelta(seconds=int(settings["stalled_after_seconds"]))
    recovered: list[str] = []
    rows = database.execute(
        "SELECT * FROM preflight_executions WHERE status='INSPECTING' ORDER BY started_at"
    ).fetchall()
    for execution in rows:
        signals: list[str] = []
        if datetime.fromisoformat(execution["heartbeat_at"]) <= stalled_cutoff:
            signals.append("heartbeat_stalled")
        if datetime.fromisoformat(execution["lease_expires_at"]) <= current:
            signals.append("lease_expired")
        if datetime.fromisoformat(execution["attempt_deadline_at"]) <= current:
            signals.append("attempt_timed_out")
        if not signals:
            continue
        reason = "Planner 预检超时恢复：" + ",".join(signals)
        database.execute(
            "UPDATE preflight_executions SET status='TIMED_OUT', finished_at=?, outcome='TIMED_OUT', "
            "termination_reason=?, recovered_at=?, recovery_action='requeue' "
            "WHERE execution_id=? AND status='INSPECTING'",
            (stamp, reason, stamp, execution["execution_id"]),
        )
        changed = database.execute(
            "UPDATE tasks SET preflight_status='UNINSPECTED', preflight_execution_id=NULL, "
            "preflight_started_at=NULL, preflight_completed_at=NULL, preflight_failure=NULL, updated_at=?, "
            "progress_summary=?, progress_next_step='等待 Planner 重新预检。', row_version=row_version+1 "
            "WHERE id=? AND status='DRAFT' AND preflight_status='INSPECTING' "
            "AND preflight_execution_id=?",
            (stamp, reason, execution["task_id"], execution["execution_id"]),
        ).rowcount
        if changed:
            database.execute(
                "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
                "VALUES(?, ?, 'DRAFT', 'DRAFT', 'planner-timeout-recovery', ?)",
                (execution["task_id"], stamp, reason),
            )
        recovered.append(execution["execution_id"])
    return recovered


def planner_task_payload(database: sqlite3.Connection, task: sqlite3.Row) -> dict[str, Any]:
    value = task_dict(database, task)
    return {
        "id": value["id"],
        "title": value["title"],
        "status": value["status"],
        "preflight_status": value["preflight_status"],
        "created_at": value["created_at"],
        "updated_at": value["updated_at"],
        "row_version": value["row_version"],
        "operator_definition": value["operator_definition"],
    }


def planner_escalation_is_approved(
    database: sqlite3.Connection, task: sqlite3.Row, escalation: str
) -> bool:
    marker = PLANNER_ESCALATION_MARKERS[escalation].casefold()
    values = [task["description"] or ""]
    values.extend(
        row[0]
        for row in database.execute(
            "SELECT text FROM task_acceptance WHERE task_id=? ORDER BY ordinal", (task["id"],)
        ).fetchall()
    )
    return any(marker == line.strip().casefold() for value in values for line in value.splitlines())


def command_preflight_claim(args: argparse.Namespace) -> None:
    if not args.execution_id or len(args.execution_id) > 128:
        raise LoopError("Planner execution-id 无效")
    config = load_initialization_config()
    boundary = config["planner"]["client_boundary"]
    if args.runtime_environment != config["planner"]["default_runtime_environment"]:
        raise LoopError("Planner runtime_environment 与初始化配置不匹配")
    if args.sandbox != boundary["sandbox"] or args.sandbox != "read-only":
        raise LoopError("Planner 必须由 read-only sandbox 入口领取")
    database = connect(args.db)
    try:
        transaction(database)
        if not uses_preflight_schema(database):
            raise LoopError("当前 Schema 不支持 Planner 预检")
        if database.execute(
            "SELECT 1 FROM preflight_executions WHERE execution_id=?", (args.execution_id,)
        ).fetchone() or database.execute(
            "SELECT 1 FROM executions WHERE execution_id=?", (args.execution_id,)
        ).fetchone():
            raise LoopError("execution-id 已存在")
        recovered = recover_timed_out_preflights(database)
        settings = config["planner"]
        active = int(database.execute(
            "SELECT count(*) FROM preflight_executions WHERE status='INSPECTING'"
        ).fetchone()[0])
        maximum = int(settings["max_active_executions"])
        if active >= maximum:
            commit(database)
            output({"outcome": "SLOT_FULL", "execution_kind": "PLANNER", "active": active,
                    "maximum": maximum, "recovered": recovered})
            return
        task = database.execute(
            "SELECT * FROM tasks WHERE status='DRAFT' AND preflight_status='UNINSPECTED' "
            "ORDER BY CASE priority WHEN 'blocker' THEN 0 WHEN 'critical' THEN 1 WHEN 'high' THEN 2 "
            "WHEN 'medium' THEN 3 ELSE 4 END, created_at, id LIMIT 1"
        ).fetchone()
        if task is None:
            commit(database)
            output({"outcome": "NO_TASK", "execution_kind": "PLANNER", "active": active,
                    "maximum": maximum, "recovered": recovered})
            return
        stamp = now_shanghai()
        lease = (datetime.fromisoformat(stamp) + timedelta(seconds=int(settings["lease_seconds"]))).isoformat(
            timespec="milliseconds"
        )
        deadline = (
            datetime.fromisoformat(stamp) + timedelta(seconds=int(settings["attempt_timeout_seconds"]))
        ).isoformat(timespec="milliseconds")
        database.execute(
            "INSERT INTO preflight_executions(execution_id, task_id, status, started_at, heartbeat_at, "
            "lease_expires_at, attempt_deadline_at, claimed_task_row_version) "
            "VALUES(?, ?, 'INSPECTING', ?, ?, ?, ?, ?)",
            (args.execution_id, task["id"], stamp, stamp, lease, deadline, task["row_version"]),
        )
        changed = database.execute(
            "UPDATE tasks SET preflight_status='INSPECTING', preflight_execution_id=?, "
            "preflight_started_at=?, preflight_completed_at=NULL, preflight_failure=NULL, updated_at=?, "
            "progress_summary=?, progress_next_step='Planner 正在进行只读静态预检。', "
            "row_version=row_version+1 WHERE id=? AND status='DRAFT' AND preflight_status='UNINSPECTED' "
            "AND row_version=?",
            (args.execution_id, stamp, stamp, f"Planner {args.execution_id} 已预留任务。",
             task["id"], task["row_version"]),
        ).rowcount
        if changed != 1:
            raise LoopError("Planner 预留时任务发生并发变化")
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, 'DRAFT', 'DRAFT', ?, 'Planner 原子预留任务并开始静态预检。')",
            (task["id"], stamp, args.execution_id),
        )
        claimed = database.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone()
        payload = planner_task_payload(database, claimed)
        revision = bump_revision(database, args.execution_id)
        commit(database)
        output({
            "outcome": "CLAIMED", "execution_kind": "PLANNER", "execution_id": args.execution_id,
            "task_id": task["id"], "lease_expires_at": lease, "attempt_deadline_at": deadline,
            "active": active + 1, "maximum": maximum, "recovered": recovered,
            "revision": revision,
            "client_boundary": {
                "sandbox": boundary["sandbox"],
                "approval_policy": boundary["approval_policy"],
                "network_access": boundary["network_access"],
                "default_tool_action": boundary["default_tool_action"],
                "source_access": boundary["source_access"],
                "writeback_transport": boundary["writeback"]["transport"],
                "allowed_writeback_commands": boundary["writeback"]["allowed_commands"],
            },
            "task": payload,
        })
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_preflight_heartbeat(args: argparse.Namespace) -> None:
    database = connect(args.db)
    try:
        transaction(database)
        recovered = recover_timed_out_preflights(database)
        execution = database.execute(
            "SELECT * FROM preflight_executions WHERE execution_id=? AND status='INSPECTING'",
            (args.execution_id,),
        ).fetchone()
        if not execution or execution["task_id"] != args.task_id:
            raise LoopError("活动 Planner execution 与 task-id 不匹配或已超时")
        task = database.execute(
            "SELECT * FROM tasks WHERE id=? AND status='DRAFT' AND preflight_status='INSPECTING' "
            "AND preflight_execution_id=?",
            (args.task_id, args.execution_id),
        ).fetchone()
        if task is None:
            raise LoopError("Planner task fencing 不匹配")
        require_expected_row_version(args, task["row_version"])
        settings = load_initialization_config()["planner"]
        stamp = now_shanghai()
        lease = (datetime.fromisoformat(stamp) + timedelta(seconds=int(settings["lease_seconds"]))).isoformat(
            timespec="milliseconds"
        )
        database.execute(
            "UPDATE preflight_executions SET heartbeat_at=?, lease_expires_at=? WHERE execution_id=?",
            (stamp, lease, args.execution_id),
        )
        database.execute(
            "UPDATE tasks SET updated_at=?, row_version=row_version+1 WHERE id=?",
            (stamp, args.task_id),
        )
        row_version = task["row_version"] + 1
        commit(database)
        output({"outcome": "HEARTBEAT", "execution_kind": "PLANNER", "task_id": args.task_id,
                "lease_expires_at": lease, "row_version": row_version, "recovered": recovered})
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def preflight_finish_context(
    database: sqlite3.Connection, args: argparse.Namespace, expected_outcome: str
) -> tuple[sqlite3.Row | None, sqlite3.Row]:
    recover_timed_out_preflights(database)
    execution = database.execute(
        "SELECT * FROM preflight_executions WHERE execution_id=?", (args.execution_id,)
    ).fetchone()
    if execution is None or execution["task_id"] != args.task_id:
        raise LoopError("Planner execution 与 task-id 不匹配")
    if execution["status"] != "INSPECTING":
        if execution["outcome"] == expected_outcome:
            return None, execution
        raise LoopError("Planner execution 已结束，迟到结果被拒绝")
    task = database.execute("SELECT * FROM tasks WHERE id=?", (args.task_id,)).fetchone()
    if not task or task["status"] != "DRAFT" or task["preflight_status"] != "INSPECTING":
        raise LoopError("任务不处于 Planner INSPECTING")
    if task["preflight_execution_id"] != args.execution_id:
        raise LoopError("Planner execution fencing 不匹配")
    require_expected_row_version(args, task["row_version"])
    return task, execution


def command_preflight_ready(args: argparse.Namespace) -> None:
    report = read_preflight_report(args.report)
    allowed = {"summary", "capability_level", "scope", "lock_mode", "technical_acceptance", "evidence"}
    if not isinstance(report, dict) or set(report) != allowed:
        raise LoopError("READY 预检结果字段无效")
    validate_preflight_text_integrity(report)
    summary = report.get("summary")
    capability = report.get("capability_level")
    lock_mode = report.get("lock_mode")
    if not isinstance(summary, str) or not summary.strip():
        raise LoopError("READY 预检 summary 不能为空")
    if capability not in CAPABILITY_LEVELS:
        raise LoopError("READY capability_level 无效")
    if lock_mode not in LOCK_MODES:
        raise LoopError("READY lock_mode 无效")
    scopes = normalize_string_list(report.get("scope"), "scope", allow_empty=False)
    technical = normalize_string_list(
        report.get("technical_acceptance"), "technical_acceptance", allow_empty=False
    )
    evidence = normalize_string_list(report.get("evidence"), "evidence", allow_empty=False)
    normalized_scopes = [normalize_scope(scope, lock_mode) for scope in scopes]
    scope_keys = [item["scope_key"] for item in normalized_scopes]
    if lock_mode != "project" and len(scope_keys) != len(set(scope_keys)):
        raise LoopError("READY scope 规范化后不能重复")
    database = connect(args.db)
    try:
        transaction(database)
        task, _ = preflight_finish_context(database, args, "READY")
        if task is None:
            commit(database)
            output({"outcome": "ALREADY_FINISHED", "execution_kind": "PLANNER",
                    "task_id": args.task_id, "preflight_status": "READY"})
            return
        if capability == "L5" and not planner_escalation_is_approved(database, task, "L5"):
            raise LoopError(
                "Planner 首次 L5 建议必须进入 NEEDS_REVIEW；缺少 Operator 记录的明确 L5 批准"
            )
        if task["execution_policy"] == "manual" and not planner_escalation_is_approved(
            database, task, "manual"
        ):
            raise LoopError(
                "Planner 首次 manual 建议必须进入 NEEDS_REVIEW；缺少 Operator 记录的明确 manual 批准"
            )
        resolve_execution_profile(task["runtime_environment"], task["provider_id"], capability)
        stamp = now_shanghai()
        database.execute("DELETE FROM task_scopes WHERE task_id=?", (args.task_id,))
        database.executemany(
            "INSERT INTO task_scopes(task_id, ordinal, scope, scope_key) VALUES(?, ?, ?, ?)",
            [
                (args.task_id, index, item["scope"], item["scope_key"])
                for index, item in enumerate(normalized_scopes)
            ],
        )
        replace_ordered_text(database, "task_technical_acceptance", args.task_id, technical)
        replace_ordered_text(database, "task_preflight_evidence", args.task_id, evidence)
        database.execute(
            "UPDATE tasks SET status='PENDING', preflight_status='READY', preflight_execution_id=NULL, "
            "preflight_completed_at=?, preflight_failure=NULL, capability_level=?, lock_mode=?, "
            "split_suggestions_json='[]', updated_at=?, progress_percent=0, progress_summary=?, "
            "progress_next_step='等待匹配的 Worker 领取。', human_required=0, human_question=NULL, "
            "human_options_json='[]', human_requested_at=NULL, human_responded_at=NULL, human_response=NULL, "
            "row_version=row_version+1 WHERE id=?",
            (stamp, capability, lock_mode, stamp, summary.strip(), args.task_id),
        )
        database.execute(
            "UPDATE preflight_executions SET status='FINISHED', finished_at=?, outcome='READY' "
            "WHERE execution_id=? AND status='INSPECTING'",
            (stamp, args.execution_id),
        )
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, 'DRAFT', 'PENDING', ?, ?)",
            (args.task_id, stamp, args.execution_id, summary.strip()),
        )
        revision = bump_revision(database, args.execution_id)
        commit(database)
        output({"outcome": "READY", "execution_kind": "PLANNER", "task_id": args.task_id,
                "status": "PENDING", "preflight_status": "READY", "revision": revision})
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_preflight_needs_review(args: argparse.Namespace) -> None:
    report = read_preflight_report(args.report)
    allowed = {"summary", "question", "options", "split_suggestions", "evidence"}
    if not isinstance(report, dict) or set(report) != allowed:
        raise LoopError("NEEDS_REVIEW 预检结果字段无效")
    validate_preflight_text_integrity(report)
    summary = report.get("summary")
    question = report.get("question")
    if not isinstance(summary, str) or not summary.strip():
        raise LoopError("NEEDS_REVIEW summary 不能为空")
    if not isinstance(question, str) or not question.strip():
        raise LoopError("NEEDS_REVIEW question 不能为空")
    options = normalize_string_list(report.get("options"), "options")
    suggestions = normalize_split_suggestions(report.get("split_suggestions"))
    evidence = normalize_string_list(report.get("evidence"), "evidence", allow_empty=False)
    database = connect(args.db)
    try:
        transaction(database)
        task, _ = preflight_finish_context(database, args, "NEEDS_REVIEW")
        if task is None:
            commit(database)
            output({"outcome": "ALREADY_FINISHED", "execution_kind": "PLANNER",
                    "task_id": args.task_id, "preflight_status": "FAILED"})
            return
        stamp = now_shanghai()
        database.execute("DELETE FROM task_scopes WHERE task_id=?", (args.task_id,))
        replace_ordered_text(database, "task_technical_acceptance", args.task_id, [])
        replace_ordered_text(database, "task_preflight_evidence", args.task_id, evidence)
        database.execute(
            "UPDATE tasks SET status='NEEDS_REVIEW', preflight_status='FAILED', "
            "preflight_execution_id=NULL, preflight_completed_at=?, preflight_failure=?, "
            "capability_level=NULL, lock_mode=NULL, split_suggestions_json=?, updated_at=?, "
            "progress_percent=0, progress_summary=?, progress_next_step='等待 Operator 取得人工决定。', "
            "human_required=1, human_question=?, human_options_json=?, human_requested_at=?, "
            "human_responded_at=NULL, human_response=NULL, row_version=row_version+1 WHERE id=?",
            (stamp, summary.strip(), json_dump(suggestions), stamp, summary.strip(), question.strip(),
             json_dump(options), stamp, args.task_id),
        )
        database.execute(
            "UPDATE preflight_executions SET status='FINISHED', finished_at=?, outcome='NEEDS_REVIEW' "
            "WHERE execution_id=? AND status='INSPECTING'",
            (stamp, args.execution_id),
        )
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, 'DRAFT', 'NEEDS_REVIEW', ?, ?)",
            (args.task_id, stamp, args.execution_id, summary.strip()),
        )
        revision = bump_revision(database, args.execution_id)
        commit(database)
        output({"outcome": "NEEDS_REVIEW", "execution_kind": "PLANNER", "task_id": args.task_id,
                "status": "NEEDS_REVIEW", "preflight_status": "FAILED", "revision": revision})
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_preflight_fail(args: argparse.Namespace) -> None:
    report = read_preflight_report(args.report)
    allowed = {"summary", "error", "evidence"}
    if not isinstance(report, dict) or set(report) != allowed:
        raise LoopError("FAILED 预检结果字段无效")
    validate_preflight_text_integrity(report)
    summary = report.get("summary")
    error = report.get("error")
    if not isinstance(summary, str) or not summary.strip() or not isinstance(error, str) or not error.strip():
        raise LoopError("FAILED 预检 summary/error 不能为空")
    evidence = normalize_string_list(report.get("evidence"), "evidence", allow_empty=False)
    database = connect(args.db)
    try:
        transaction(database)
        task, _ = preflight_finish_context(database, args, "FAILED")
        if task is None:
            commit(database)
            output({"outcome": "ALREADY_FINISHED", "execution_kind": "PLANNER",
                    "task_id": args.task_id, "preflight_status": "FAILED"})
            return
        stamp = now_shanghai()
        database.execute("DELETE FROM task_scopes WHERE task_id=?", (args.task_id,))
        replace_ordered_text(database, "task_technical_acceptance", args.task_id, [])
        replace_ordered_text(database, "task_preflight_evidence", args.task_id, evidence)
        database.execute(
            "UPDATE tasks SET status='NEEDS_REVIEW', preflight_status='FAILED', "
            "preflight_execution_id=NULL, preflight_completed_at=?, preflight_failure=?, "
            "capability_level=NULL, lock_mode=NULL, split_suggestions_json='[]', updated_at=?, "
            "progress_percent=0, progress_summary=?, progress_next_step='等待 Operator 修正后重新预检。', "
            "human_required=1, human_question='Planner 预检失败；是否修正任务定义后重新预检？', "
            "human_options_json='[\"修正后重新预检\",\"取消任务\"]', human_requested_at=?, "
            "human_responded_at=NULL, human_response=NULL, row_version=row_version+1 WHERE id=?",
            (stamp, error.strip(), stamp, summary.strip(), stamp, args.task_id),
        )
        database.execute(
            "UPDATE preflight_executions SET status='FAILED', finished_at=?, outcome='FAILED', "
            "termination_reason=? WHERE execution_id=? AND status='INSPECTING'",
            (stamp, error.strip(), args.execution_id),
        )
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, 'DRAFT', 'NEEDS_REVIEW', ?, ?)",
            (args.task_id, stamp, args.execution_id, summary.strip()),
        )
        revision = bump_revision(database, args.execution_id)
        commit(database)
        output({"outcome": "FAILED", "execution_kind": "PLANNER", "task_id": args.task_id,
                "status": "NEEDS_REVIEW", "preflight_status": "FAILED", "revision": revision})
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def task_scopes_and_conflicts(
    database: sqlite3.Connection, task_id: str
) -> tuple[list[sqlite3.Row], list[dict[str, Any]]]:
    scopes = database.execute(
        "SELECT DISTINCT scope_key FROM task_scopes WHERE task_id=? ORDER BY scope_key",
        (task_id,),
    ).fetchall()
    return scopes, scope_conflicts_for_keys(database, [scope["scope_key"] for scope in scopes])


def describe_conflicting_task(
    task_id: str,
    conflicts: list[dict[str, Any]],
) -> dict[str, Any]:
    return {"task_id": task_id, "conflicts": conflicts}


def scope_lock_credential(
    database: sqlite3.Connection,
    execution_id: str,
    task_id: str,
) -> dict[str, Any]:
    task_columns = {row[1] for row in database.execute("PRAGMA table_info(tasks)")}
    task = (
        database.execute("SELECT lock_mode FROM tasks WHERE id=?", (task_id,)).fetchone()
        if "lock_mode" in task_columns else None
    )
    lock_columns = {row[1] for row in database.execute("PRAGMA table_info(scope_locks)")}
    status_projection = "status" if "status" in lock_columns else "'ACTIVE' AS status"
    locks = database.execute(
        f"SELECT scope_key, {status_projection}, acquired_at, lease_expires_at FROM scope_locks "
        "WHERE execution_id=? AND task_id=? ORDER BY scope_key",
        (execution_id, task_id),
    ).fetchall()
    return {
        "execution_id": execution_id,
        "task_id": task_id,
        "lock_mode": task["lock_mode"] if task is not None else "project",
        "scope_keys": [row["scope_key"] for row in locks],
        "locks": [dict(row) for row in locks],
    }


def claim_target(args: argparse.Namespace) -> dict[str, Any]:
    if args.profile:
        capability_level = LEGACY_PROFILE_TO_CAPABILITY[args.profile]
        inferred_policy = "manual" if args.profile == "exceptional" else "automatic"
        if args.execution_policy and args.execution_policy != inferred_policy:
            raise LoopError("旧 --profile 与 --execution-policy 不一致")
        execution_policy = inferred_policy
    else:
        capability_level = args.capability_level
        execution_policy = args.execution_policy or "automatic"
    runtime_environment, provider_id = normalize_execution_target(
        args.runtime_environment, args.provider_id
    )
    snapshot = resolve_execution_profile(runtime_environment, provider_id, capability_level)
    return {
        **snapshot,
        "execution_policy": execution_policy,
        "execution_profile": legacy_profile_for(capability_level, execution_policy),
        "requested_runtime_environment": args.runtime_environment,
    }


def command_claim(args: argparse.Namespace) -> None:
    if not args.execution_id or len(args.execution_id) > 128:
        raise LoopError("execution-id 无效")
    target = claim_target(args)
    database = connect(args.db)
    try:
        transaction(database)
        capability_schema = uses_capability_schema(database)
        preflight_schema = uses_preflight_schema(database)
        if database.execute("SELECT 1 FROM executions WHERE execution_id=?", (args.execution_id,)).fetchone():
            raise LoopError("execution-id 已存在")
        transition_recovery_states(database, stalled_executions(database))
        recovery_required = recovery_required_records(database)
        compatible_recoveries = [
            item for item in recovery_required
            if item["runtime_environment"] == target["runtime_environment"]
            and item["provider_id"] == target["provider_id"]
            and item["capability_level"] == target["capability_level"]
            and item["execution_policy"] == target["execution_policy"]
        ]
        requeued = requeue_resolved_conflicts(database)
        active = database.execute("SELECT count(*) FROM executions WHERE status='RUNNING'").fetchone()[0]
        maximum = global_parallel_limit()
        if active >= maximum:
            commit(database)
            output({
                "outcome": "SLOT_FULL", "limit_scope": "global",
                "profile": target["execution_profile"], "capability_level": target["capability_level"],
                "runtime_environment": target["runtime_environment"], "provider_id": target["provider_id"],
                "active": active, "maximum": maximum, "recovery_required": compatible_recoveries,
            })
            return
        if capability_schema:
            platform_active = database.execute(
                "SELECT count(*) FROM executions WHERE status='RUNNING' AND runtime_environment=?",
                (target["runtime_environment"],),
            ).fetchone()[0]
        else:
            legacy_environment = (
                "deepseek" if target["runtime_environment"] == "self_hosted_agent"
                else target["runtime_environment"]
            )
            platform_active = database.execute(
                "SELECT count(*) FROM executions e JOIN tasks t ON t.id=e.task_id "
                "WHERE e.status='RUNNING' AND t.runtime_environment=?",
                (legacy_environment,),
            ).fetchone()[0]
        platform_maximum = platform_parallel_limit(target["runtime_environment"])
        if platform_active >= platform_maximum:
            commit(database)
            output({
                "outcome": "SLOT_FULL", "limit_scope": "platform",
                "profile": target["execution_profile"], "capability_level": target["capability_level"],
                "runtime_environment": target["runtime_environment"], "provider_id": target["provider_id"],
                "active": active, "maximum": maximum, "platform_active": platform_active,
                "platform_maximum": platform_maximum, "recovery_required": compatible_recoveries,
            })
            return
        if preflight_schema:
            candidates = database.execute(
                "SELECT * FROM tasks WHERE status='PENDING' AND preflight_status='READY' "
                "AND runtime_environment=? AND provider_id IS ? AND capability_level=? AND execution_policy=? "
                "AND lock_mode IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM task_scopes s WHERE s.task_id=tasks.id) "
                "AND EXISTS (SELECT 1 FROM task_technical_acceptance a WHERE a.task_id=tasks.id) "
                "AND EXISTS (SELECT 1 FROM task_preflight_evidence e WHERE e.task_id=tasks.id) ORDER BY "
                "CASE priority WHEN 'blocker' THEN 0 WHEN 'critical' THEN 1 WHEN 'high' THEN 2 "
                "WHEN 'medium' THEN 3 ELSE 4 END, created_at, id",
                (
                    target["runtime_environment"], target["provider_id"], target["capability_level"],
                    target["execution_policy"],
                ),
            ).fetchall()
        elif capability_schema:
            candidates = database.execute(
                "SELECT * FROM tasks WHERE status='PENDING' AND runtime_environment=? AND provider_id IS ? "
                "AND capability_level=? AND execution_policy=? ORDER BY "
                "CASE priority WHEN 'blocker' THEN 0 WHEN 'critical' THEN 1 WHEN 'high' THEN 2 "
                "WHEN 'medium' THEN 3 ELSE 4 END, created_at, id",
                (
                    target["runtime_environment"], target["provider_id"], target["capability_level"],
                    target["execution_policy"],
                ),
            ).fetchall()
        else:
            if target["execution_policy"] == "manual" and target["capability_level"] != "L5":
                raise LoopError("Schema 3.3.0 兼容层无法表示 L1-L4 manual execution_policy")
            legacy_environment = (
                "deepseek" if target["runtime_environment"] == "self_hosted_agent" else target["runtime_environment"]
            )
            if target["runtime_environment"] == "self_hosted_agent" and target["provider_id"] != "deepseek":
                candidates = []
            else:
                candidates = database.execute(
                    "SELECT * FROM tasks WHERE status='PENDING' AND runtime_environment=? AND execution_profile=? "
                    "ORDER BY CASE priority WHEN 'blocker' THEN 0 WHEN 'critical' THEN 1 WHEN 'high' THEN 2 "
                    "WHEN 'medium' THEN 3 ELSE 4 END, created_at, id",
                    (legacy_environment, target["execution_profile"]),
                ).fetchall()
        task_row = None
        scopes: list[sqlite3.Row] = []
        deferred_conflicts: list[dict[str, Any]] = []
        stamp = now_shanghai()
        for candidate in candidates:
            if not dependencies_ready(database, candidate["id"]):
                continue
            candidate_scopes, conflicts = task_scopes_and_conflicts(database, candidate["id"])
            if conflicts:
                deferred_conflicts.append(
                    describe_conflicting_task(candidate["id"], conflicts)
                )
                continue
            task_row = candidate
            scopes = candidate_scopes
            break
        if task_row is None:
            if deferred_conflicts:
                revision = bump_revision(database, "concurrent-claimer")
                commit(database)
                output(
                    {
                        "outcome": "CONFLICT",
                        "profile": target["execution_profile"],
                        "capability_level": target["capability_level"],
                        "runtime_environment": target["runtime_environment"],
                        "provider_id": target["provider_id"],
                        "task_id": deferred_conflicts[0]["task_id"],
                        "conflicts": deferred_conflicts[0]["conflicts"],
                        "deferred_conflicts": deferred_conflicts,
                        "recovery_required": compatible_recoveries,
                        "requeued": requeued,
                        "revision": revision,
                    }
                )
                return
            outcome = (
                "RECOVERY_REQUIRED"
                if compatible_recoveries and target["runtime_environment"] == "codex_automation"
                else "NO_TASK"
            )
            commit(database)
            output({
                "outcome": outcome, "profile": target["execution_profile"],
                "capability_level": target["capability_level"],
                "runtime_environment": target["runtime_environment"], "provider_id": target["provider_id"],
                "active": active,
                "recovery_required": compatible_recoveries, "requeued": requeued,
            })
            return
        lease_seconds = int(execution_setting("task_lease_seconds", 3600))
        expiry = expires_at(lease_seconds)
        if capability_schema:
            database.execute(
                """INSERT INTO executions(
                  execution_id, task_id, status, started_at, heartbeat_at, lease_expires_at,
                  runtime_environment, provider_id, capability_level, execution_policy, model, reasoning,
                  attempt_timeout_seconds, max_retries
                ) VALUES(?, ?, 'RUNNING', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    args.execution_id, task_row["id"], stamp, stamp, expiry,
                    target["runtime_environment"], target["provider_id"], target["capability_level"],
                    target["execution_policy"], target["model"], target["reasoning"],
                    target["attempt_timeout_seconds"], target["max_retries"],
                ),
            )
        else:
            database.execute(
                "INSERT INTO executions(execution_id, task_id, status, started_at, heartbeat_at, lease_expires_at) "
                "VALUES(?, ?, 'RUNNING', ?, ?, ?)",
                (args.execution_id, task_row["id"], stamp, stamp, expiry),
            )
        for scope in scopes:
            database.execute(
                "INSERT INTO scope_locks(scope_key, task_id, execution_id, acquired_at, lease_expires_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (scope["scope_key"], task_row["id"], args.execution_id, stamp, expiry),
            )
        diagnostic_reset = "result_diagnostic_json=NULL, " if uses_result_diagnostic_schema(database) else ""
        database.execute(
            f"UPDATE tasks SET status='RUNNING', assigned_agent=?, started_at=?, updated_at=?, heartbeat_at=?, "
            "completed_at=NULL, attempt=attempt+1, progress_summary=?, progress_next_step=?, "
            f"result_summary=NULL, result_error=NULL, {diagnostic_reset}human_required=0, human_question=NULL, "
            "human_options_json='[]', human_requested_at=NULL, human_responded_at=NULL, human_response=NULL, "
            "row_version=row_version+1 WHERE id=?",
            (
                args.execution_id,
                stamp,
                stamp,
                stamp,
                f"并发执行 {args.execution_id} 已领取任务。",
                "在当前自动化对话中执行并验证。",
                task_row["id"],
            ),
        )
        database.execute("DELETE FROM task_verifications WHERE task_id=?", (task_row["id"],))
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, 'PENDING', 'RUNNING', ?, '并发 Worker 原子领取任务并获取 scope 锁。')",
            (task_row["id"], stamp, args.execution_id),
        )
        revision = bump_revision(database, args.execution_id)
        claimed = database.execute("SELECT * FROM tasks WHERE id=?", (task_row["id"],)).fetchone()
        payload = task_dict(database, claimed)
        lock_credential = scope_lock_credential(database, args.execution_id, task_row["id"])
        if target["requested_runtime_environment"] == "deepseek":
            payload["canonical_runtime_environment"] = payload["runtime_environment"]
            payload["runtime_environment"] = "deepseek"
        commit(database)
        output(
            {
                "outcome": "CLAIMED",
                "execution_id": args.execution_id,
                "lease_expires_at": expiry,
                "active": active + 1,
                "maximum": maximum,
                "profile": target["execution_profile"],
                "capability_level": target["capability_level"],
                "execution_policy": target["execution_policy"],
                "runtime_environment": target["runtime_environment"],
                "provider_id": target["provider_id"],
                "platform_active": platform_active + 1,
                "platform_maximum": platform_maximum,
                "revision": revision,
                "deferred_conflicts": deferred_conflicts,
                "recovery_required": compatible_recoveries,
                "requeued": requeued,
                "scope_lock_credential": lock_credential,
                "task": payload,
            }
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_heartbeat(args: argparse.Namespace) -> None:
    database = connect(args.db)
    try:
        transaction(database)
        execution = database.execute(
            "SELECT * FROM executions WHERE execution_id=? AND status='RUNNING'", (args.execution_id,)
        ).fetchone()
        if not execution or execution["task_id"] != args.task_id:
            raise LoopError("活动 execution 与 task-id 不匹配")
        stamp = now_shanghai()
        expiry = expires_at(int(execution_setting("task_lease_seconds", 3600)))
        database.execute(
            "UPDATE executions SET heartbeat_at=?, lease_expires_at=? WHERE execution_id=?",
            (stamp, expiry, args.execution_id),
        )
        lock_update = (
            "UPDATE scope_locks SET lease_expires_at=? WHERE execution_id=? AND status='ACTIVE'"
            if uses_recovery_schema(database)
            else "UPDATE scope_locks SET lease_expires_at=? WHERE execution_id=?"
        )
        database.execute(lock_update, (expiry, args.execution_id))
        database.execute(
            "UPDATE tasks SET heartbeat_at=?, updated_at=?, row_version=row_version+1 WHERE id=? AND status='RUNNING'",
            (stamp, stamp, args.task_id),
        )
        credential = scope_lock_credential(database, args.execution_id, args.task_id)
        commit(database)
        output({
            "outcome": "HEARTBEAT",
            "task_id": args.task_id,
            "lease_expires_at": expiry,
            "scope_lock_credential": credential,
        })
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_extend_scope(args: argparse.Namespace) -> None:
    report = read_json_source(args.report)
    if not isinstance(report, dict) or set(report) != {"scope"}:
        raise LoopError("scope 扩展结果必须只包含 scope")
    requested_scopes = normalize_string_list(report.get("scope"), "scope", allow_empty=False)
    database = connect(args.db)
    try:
        transaction(database)
        execution = database.execute(
            "SELECT * FROM executions WHERE execution_id=? AND status='RUNNING'",
            (args.execution_id,),
        ).fetchone()
        if execution is None or execution["task_id"] != args.task_id:
            raise LoopError("活动 execution 与 task-id 不匹配")
        task = database.execute(
            "SELECT status, assigned_agent, lock_mode, row_version FROM tasks WHERE id=?",
            (args.task_id,),
        ).fetchone()
        if (
            task is None
            or task["status"] != "RUNNING"
            or task["assigned_agent"] != args.execution_id
            or task["lock_mode"] not in LOCK_MODES
        ):
            raise LoopError("任务不具备活动 scope 扩展资格")
        require_expected_row_version(args, task["row_version"])
        normalized = [normalize_scope(scope, task["lock_mode"]) for scope in requested_scopes]
        canonical = [item["scope"].casefold() for item in normalized]
        if len(canonical) != len(set(canonical)):
            raise LoopError("scope 扩展项规范化后不能重复")
        existing_rows = database.execute(
            "SELECT scope, scope_key FROM task_scopes WHERE task_id=? ORDER BY ordinal",
            (args.task_id,),
        ).fetchall()
        existing_scopes = {row["scope"].casefold() for row in existing_rows}
        additions = [item for item in normalized if item["scope"].casefold() not in existing_scopes]
        new_keys = sorted({item["scope_key"] for item in additions})
        conflicts = scope_conflicts_for_keys(
            database, new_keys, exclude_execution_id=args.execution_id
        )
        if conflicts:
            credential = scope_lock_credential(database, args.execution_id, args.task_id)
            commit(database)
            output({
                "outcome": "SCOPE_EXTENSION_CONFLICT",
                "task_id": args.task_id,
                "execution_id": args.execution_id,
                "conflicts": conflicts,
                "scope_lock_credential": credential,
            })
            return
        if not additions:
            credential = scope_lock_credential(database, args.execution_id, args.task_id)
            commit(database)
            output({
                "outcome": "SCOPE_ALREADY_REGISTERED",
                "task_id": args.task_id,
                "execution_id": args.execution_id,
                "scope_lock_credential": credential,
            })
            return
        stamp = now_shanghai()
        next_ordinal = int(database.execute(
            "SELECT COALESCE(max(ordinal), -1) + 1 FROM task_scopes WHERE task_id=?",
            (args.task_id,),
        ).fetchone()[0])
        database.executemany(
            "INSERT INTO task_scopes(task_id, ordinal, scope, scope_key) VALUES(?, ?, ?, ?)",
            [
                (args.task_id, next_ordinal + index, item["scope"], item["scope_key"])
                for index, item in enumerate(additions)
            ],
        )
        held_keys = {
            row[0] for row in database.execute(
                "SELECT scope_key FROM scope_locks WHERE execution_id=?", (args.execution_id,)
            ).fetchall()
        }
        expiry = execution["lease_expires_at"]
        database.executemany(
            "INSERT INTO scope_locks(scope_key, task_id, execution_id, acquired_at, lease_expires_at) "
            "VALUES(?, ?, ?, ?, ?)",
            [
                (scope_key, args.task_id, args.execution_id, stamp, expiry)
                for scope_key in new_keys if scope_key not in held_keys
            ],
        )
        database.execute(
            "UPDATE tasks SET updated_at=?, progress_summary='execution 已原子扩展 scope 锁。', "
            "row_version=row_version+1 WHERE id=?",
            (stamp, args.task_id),
        )
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, 'RUNNING', 'RUNNING', ?, 'execution 在修改新范围前原子扩展 scope 锁。')",
            (args.task_id, stamp, args.execution_id),
        )
        credential = scope_lock_credential(database, args.execution_id, args.task_id)
        revision = bump_revision(database, args.execution_id)
        commit(database)
        output({
            "outcome": "SCOPE_EXTENDED",
            "task_id": args.task_id,
            "execution_id": args.execution_id,
            "added_scope": [item["scope"] for item in additions],
            "scope_lock_credential": credential,
            "revision": revision,
        })
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_finish(args: argparse.Namespace) -> None:
    report = read_json_source(args.report)
    status = report.get("status")
    if status not in FINAL_EXECUTION_STATUSES:
        raise LoopError("报告 status 仅支持 SUCCEEDED、FAILED、WAITING_HUMAN")
    if not str(report.get("summary") or "").strip():
        raise LoopError("报告 summary 不能为空")
    if status == "SUCCEEDED" and not report.get("verification"):
        raise LoopError("SUCCEEDED 必须提供 verification")
    if status == "FAILED" and not str(report.get("error") or "").strip():
        raise LoopError("FAILED 必须提供 error")
    if status == "WAITING_HUMAN" and not str(report.get("question") or "").strip():
        raise LoopError("WAITING_HUMAN 必须提供 question")
    diagnostic = normalize_result_diagnostic(report.get("diagnostic"))
    if status == "SUCCEEDED" and diagnostic is not None:
        raise LoopError("SUCCEEDED 不得包含 result diagnostic")
    diagnostic_json = json_dump(diagnostic) if diagnostic is not None else None
    database = connect(args.db)
    try:
        transaction(database)
        execution = database.execute(
            "SELECT * FROM executions WHERE execution_id=? AND status='RUNNING'", (args.execution_id,)
        ).fetchone()
        if not execution or execution["task_id"] != args.task_id:
            raise LoopError("活动 execution 与 task-id 不匹配")
        task = database.execute("SELECT status FROM tasks WHERE id=?", (args.task_id,)).fetchone()
        if not task or task["status"] != "RUNNING":
            raise LoopError("任务不处于 RUNNING")
        stamp = now_shanghai()
        waiting = status == "WAITING_HUMAN"
        diagnostic_column = "result_diagnostic_json=?, " if uses_result_diagnostic_schema(database) else ""
        values: list[Any] = [
            status,
            stamp,
            stamp,
            None if waiting else stamp,
            max(0, min(99, int(report.get("percent", 0)))) if waiting else 100,
            report["summary"],
            report.get("next_step", "等待人工答复。") if waiting else None,
            report["summary"] if status == "SUCCEEDED" else None,
            report.get("error") if status == "FAILED" else None,
        ]
        if diagnostic_column:
            values.append(diagnostic_json)
        values.extend([
            int(waiting),
            report.get("question") if waiting else None,
            json_dump(report.get("options") or []),
            stamp if waiting else None,
            args.task_id,
        ])
        database.execute(
            f"""
            UPDATE tasks SET status=?, updated_at=?, heartbeat_at=?, completed_at=?,
              progress_percent=?, progress_summary=?, progress_next_step=?,
              result_summary=?, result_error=?, {diagnostic_column}human_required=?, human_question=?,
              human_options_json=?, human_requested_at=?, human_responded_at=NULL,
              human_response=NULL, row_version=row_version+1
            WHERE id=?
            """,
            values,
        )
        if "completed" in report:
            replace_ordered_text(database, "task_completed_items", args.task_id, report.get("completed") or [])
        replace_ordered_text(database, "task_verifications", args.task_id, report.get("verification") or [])
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, 'RUNNING', ?, ?, ?)",
            (args.task_id, stamp, status, args.execution_id, report.get("reason") or report["summary"]),
        )
        database.execute(
            "UPDATE executions SET status='FINISHED', finished_at=?, outcome=? WHERE execution_id=?",
            (stamp, status, args.execution_id),
        )
        database.execute("DELETE FROM scope_locks WHERE execution_id=?", (args.execution_id,))
        requeued: list[str] = []
        revision = bump_revision(database, args.execution_id)
        commit(database)
        output(
            {
                "outcome": "FINISHED",
                "task_id": args.task_id,
                "status": status,
                "requeued_conflicts": requeued,
                "revision": revision,
            }
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_confirm(args: argparse.Namespace) -> None:
    database = connect(args.db)
    try:
        transaction(database)
        task = database.execute(
            "SELECT status, archived_at, row_version FROM tasks WHERE id=?", (args.task_id,)
        ).fetchone()
        if task:
            require_expected_row_version(args, task["row_version"])
        if not task or task["status"] != "SUCCEEDED":
            raise LoopError("只有 SUCCEEDED 任务可以人工确认")
        if task["archived_at"] is not None:
            raise LoopError("已归档任务必须先取消归档")
        stamp = now_shanghai()
        reason = args.reason or "人工复核通过，任务已确认。"
        database.execute(
            "UPDATE tasks SET status='CONFIRMED', updated_at=?, progress_percent=100, "
            "progress_summary='人工复核通过，任务已确认。', progress_next_step=NULL, "
            "row_version=row_version+1 WHERE id=?",
            (stamp, args.task_id),
        )
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, 'SUCCEEDED', 'CONFIRMED', 'human-review', ?)",
            (args.task_id, stamp, reason),
        )
        revision = bump_revision(database, "human-review")
        commit(database)
        output(
            {
                "outcome": "CONFIRMED",
                "task_id": args.task_id,
                "row_version": task["row_version"] + 1,
                "revision": revision,
            }
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_resolve_human(args: argparse.Namespace) -> None:
    response = str(args.response or "").strip()
    if not response:
        raise LoopError("人工答复不能为空")
    database = connect(args.db)
    try:
        transaction(database)
        task = database.execute("SELECT * FROM tasks WHERE id=?", (args.task_id,)).fetchone()
        if not task:
            raise LoopError("任务不存在")
        require_expected_row_version(args, task["row_version"])
        if task["archived_at"] is not None:
            raise LoopError("已归档任务必须先取消归档")
        if task["status"] not in {"WAITING_HUMAN", "PENDING"}:
            raise LoopError("只有等待人工的任务可以由人工答复直接完成")
        if database.execute(
            "SELECT 1 FROM executions WHERE task_id=? AND status='RUNNING'", (args.task_id,)
        ).fetchone():
            raise LoopError("任务存在活动 execution，不能由人工答复直接完成")
        if database.execute(
            "SELECT 1 FROM scope_locks WHERE task_id=?", (args.task_id,)
        ).fetchone():
            raise LoopError("任务仍持有 scope 锁，不能由人工答复直接完成")

        if task["status"] == "PENDING":
            latest_history = database.execute(
                "SELECT from_status, to_status FROM task_history WHERE task_id=? ORDER BY id DESC LIMIT 1",
                (args.task_id,),
            ).fetchone()
            if not latest_history or tuple(latest_history) != ("WAITING_HUMAN", "PENDING"):
                raise LoopError("PENDING 任务仅可在刚从 WAITING_HUMAN 误重排且尚未再次领取时直接完成")
            if not str(args.summary or "").strip():
                raise LoopError("已重新排队的任务直接完成时必须提供 summary")
        elif not task["human_required"]:
            raise LoopError("任务没有待解决的人工问题")

        verification_count = database.execute(
            "SELECT count(*) FROM task_verifications WHERE task_id=?", (args.task_id,)
        ).fetchone()[0]
        if verification_count < 1:
            raise LoopError("缺少 Worker 验证记录，不能仅凭人工答复直接完成")

        summary = str(args.summary or task["progress_summary"] or "").strip()
        if not summary:
            raise LoopError("完成摘要不能为空")
        stamp = now_shanghai()
        previous_status = task["status"]
        diagnostic_reset = "result_diagnostic_json=NULL, " if uses_result_diagnostic_schema(database) else ""
        database.execute(
            f"""
            UPDATE tasks SET status='SUCCEEDED', updated_at=?, completed_at=?,
              progress_percent=100, progress_summary=?, progress_next_step=NULL,
              result_summary=?, result_error=NULL, {diagnostic_reset}human_required=0,
              human_responded_at=?, human_response=?, row_version=row_version+1
            WHERE id=?
            """,
            (stamp, stamp, summary, summary, stamp, response, args.task_id),
        )
        reason = args.reason or f"人工答复已解决最后阻塞项：{response}"
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, ?, 'SUCCEEDED', 'human-resolution', ?)",
            (args.task_id, stamp, previous_status, reason),
        )
        requeued: list[str] = []
        revision = bump_revision(database, "human-resolution")
        commit(database)
        output(
            {
                "outcome": "HUMAN_RESOLVED",
                "task_id": args.task_id,
                "status": "SUCCEEDED",
                "row_version": task["row_version"] + 1,
                "requeued_conflicts": requeued,
                "revision": revision,
            }
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_archive(args: argparse.Namespace) -> None:
    database = connect(args.db)
    try:
        transaction(database)
        task = database.execute(
            "SELECT status, archived_at, row_version FROM tasks WHERE id=?", (args.task_id,)
        ).fetchone()
        if not task:
            raise LoopError("任务不存在")
        require_expected_row_version(args, task["row_version"])
        if task["status"] not in ARCHIVABLE_STATUSES:
            allowed = "、".join(sorted(ARCHIVABLE_STATUSES))
            raise LoopError(f"只有终态任务可以归档；允许状态：{allowed}")
        if task["archived_at"] is not None:
            commit(database)
            output(
                {
                    "outcome": "ALREADY_ARCHIVED",
                    "task_id": args.task_id,
                    "status": task["status"],
                    "archived_at": task["archived_at"],
                    "row_version": task["row_version"],
                    "revision": bump_revision(database, "task-manager"),
                }
            )
            return
        stamp = now_shanghai()
        reason = args.reason or "人工归档任务。"
        database.execute(
            "UPDATE tasks SET archived_at=?, row_version=row_version+1 WHERE id=?",
            (stamp, args.task_id),
        )
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, ?, ?, 'task-manager', ?)",
            (args.task_id, stamp, task["status"], task["status"], reason),
        )
        revision = bump_revision(database, "task-manager")
        commit(database)
        output(
            {
                "outcome": "ARCHIVED",
                "task_id": args.task_id,
                "status": task["status"],
                "archived_at": stamp,
                "row_version": task["row_version"] + 1,
                "revision": revision,
            }
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_unarchive(args: argparse.Namespace) -> None:
    database = connect(args.db)
    try:
        transaction(database)
        task = database.execute(
            "SELECT status, archived_at FROM tasks WHERE id=?", (args.task_id,)
        ).fetchone()
        if not task:
            raise LoopError("任务不存在")
        if task["archived_at"] is None:
            commit(database)
            output(
                {
                    "outcome": "ALREADY_UNARCHIVED",
                    "task_id": args.task_id,
                    "status": task["status"],
                    "archived_at": None,
                    "revision": bump_revision(database, "task-manager"),
                }
            )
            return
        stamp = now_shanghai()
        reason = args.reason or "人工取消归档。"
        database.execute("UPDATE tasks SET archived_at=NULL WHERE id=?", (args.task_id,))
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, ?, ?, 'task-manager', ?)",
            (args.task_id, stamp, task["status"], task["status"], reason),
        )
        revision = bump_revision(database, "task-manager")
        commit(database)
        output(
            {
                "outcome": "UNARCHIVED",
                "task_id": args.task_id,
                "status": task["status"],
                "archived_at": None,
                "revision": revision,
            }
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_enqueue(args: argparse.Namespace) -> None:
    payload = read_json(Path(args.file).resolve())
    tasks = payload if isinstance(payload, list) else payload.get("tasks", [payload])
    database = connect(args.db)
    try:
        transaction(database)
        stamp = now_shanghai()
        added: list[str] = []
        for item in tasks:
            requested_status = item.get("status", "DRAFT")
            if requested_status != "DRAFT":
                raise LoopError("新任务必须以 DRAFT/UNINSPECTED 创建并经过 Planner 预检")
            forbidden = {
                "preflight_status", "preflight_execution_id", "preflight_started_at",
                "preflight_completed_at", "preflight_failure", "lock_mode",
                "technical_acceptance", "preflight_evidence", "split_suggestions",
            }
            if set(item) & forbidden:
                raise LoopError("新任务不得预写 Planner 补充字段")
            estimate = item.get("estimated_capability_level", item.get("capability_level"))
            task = {
                **item,
                "status": "DRAFT",
                "preflight_status": "UNINSPECTED",
                "estimated_capability_level": estimate,
                "capability_level": None,
                "scope_hint": item.get("scope_hint", item.get("scope") or []),
                "scope": [],
                "created_at": item.get("created_at", stamp),
                "updated_at": stamp,
                "progress": item.get("progress")
                or {
                    "percent": 0,
                    "summary": "任务已创建，等待 Planner 静态预检。",
                    "completed": [],
                    "next_step": "等待 Planner 原子预留。",
                },
                "result": item.get("result") or {"summary": None, "verification": [], "error": None},
            }
            insert_task(
                database,
                task,
                actor="task-manager",
                project_paths=[item["path"] for item in configured_projects()],
            )
            added.append(task["id"])
        revision = bump_revision(database, "task-manager")
        commit(database)
        output({"outcome": "ENQUEUED", "task_ids": added, "revision": revision})
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_update(args: argparse.Namespace) -> None:
    patch = read_json(Path(args.file).resolve())
    allowed = {
        "title", "description", "priority", "execution_profile", "capability_level",
        "estimated_capability_level",
        "runtime_environment", "provider_id", "execution_policy",
    }
    unknown = set(patch) - allowed - {"scope", "scope_hint", "depends_on", "acceptance"}
    if unknown:
        raise LoopError("不支持的更新字段: " + ", ".join(sorted(unknown)))
    if not patch:
        raise LoopError("任务更新不能为空")
    if "capability_level" in patch and "estimated_capability_level" in patch:
        if patch["capability_level"] != patch["estimated_capability_level"]:
            raise LoopError("capability_level 兼容输入与 estimated_capability_level 不一致")
    database = connect(args.db)
    try:
        transaction(database)
        task = database.execute("SELECT * FROM tasks WHERE id=?", (args.task_id,)).fetchone()
        if not task:
            raise LoopError("任务不存在")
        if task["status"] in {"RUNNING", "CONFIRMED", "CANCELLED"}:
            raise LoopError(f"{task['status']} 任务不能修改")
        if uses_preflight_schema(database) and task["preflight_status"] == "INSPECTING":
            raise LoopError("Planner 正在预检，任务不能并发修改")
        if task["archived_at"] is not None:
            raise LoopError("已归档任务必须先取消归档")
        require_expected_row_version(args, task["row_version"])
        if "priority" in patch and patch["priority"] not in PRIORITIES:
            raise LoopError(f"任务优先级无效: {patch['priority']}")
        if not uses_preflight_schema(database):
            raise LoopError("当前 Schema 不支持 Planner 任务更新契约")
        current_payload = task_dict(database, task)
        execution_profile = patch.get("execution_profile")
        if execution_profile is not None and execution_profile not in EXECUTION_PROFILES:
            raise LoopError(f"执行档位无效: {execution_profile}")
        estimated_capability_level = patch.get(
            "estimated_capability_level",
            patch.get(
                "capability_level",
                LEGACY_PROFILE_TO_CAPABILITY[execution_profile]
                if execution_profile else current_payload["estimated_capability_level"],
            ),
        )
        execution_policy = patch.get(
            "execution_policy",
            ("manual" if execution_profile == "exceptional" else "automatic")
            if execution_profile else current_payload["execution_policy"],
        )
        if estimated_capability_level is not None and estimated_capability_level not in CAPABILITY_LEVELS:
            raise LoopError("estimated_capability_level 无效")
        if execution_policy not in {"automatic", "manual"}:
            raise LoopError("execution_policy 无效")
        if execution_profile and legacy_profile_for(estimated_capability_level, execution_policy) != execution_profile:
            raise LoopError("旧 execution_profile 与 capability_level/execution_policy 不一致")
        if execution_policy == "manual" and estimated_capability_level not in {None, "L5"}:
            raise LoopError("人工执行策略只允许 L5")
        runtime_environment = patch.get("runtime_environment", current_payload["runtime_environment"])
        provider_id = patch.get("provider_id", current_payload["provider_id"])
        if patch.get("runtime_environment") == "deepseek" and "provider_id" not in patch:
            provider_id = "deepseek"
        if runtime_environment != "self_hosted_agent" and runtime_environment != "deepseek" and "provider_id" not in patch:
            provider_id = None
        runtime_environment, provider_id = normalize_execution_target(runtime_environment, provider_id)
        if estimated_capability_level is not None:
            resolve_execution_profile(runtime_environment, provider_id, estimated_capability_level)
        scope_hint = normalize_string_list(
            patch.get("scope_hint", patch.get("scope", current_payload["scope_hint"])), "scope_hint"
        )
        if "scope_hint" in patch or "scope" in patch:
            for scope in scope_hint:
                resolve_scope_key(scope)
        title = patch.get("title", task["title"])
        description = patch.get("description", task["description"])
        priority = patch.get("priority", task["priority"])
        if not isinstance(title, str) or not title.strip():
            raise LoopError("title 不能为空")
        if not isinstance(description, str):
            raise LoopError("description 必须是字符串")
        stamp = now_shanghai()
        previous_status = task["status"]
        database.execute("DELETE FROM task_conflicts WHERE task_id=?", (args.task_id,))
        database.execute("DELETE FROM task_scopes WHERE task_id=?", (args.task_id,))
        replace_ordered_text(database, "task_technical_acceptance", args.task_id, [])
        replace_ordered_text(database, "task_preflight_evidence", args.task_id, [])
        if "acceptance" in patch:
            replace_ordered_text(
                database, "task_acceptance", args.task_id,
                normalize_string_list(patch["acceptance"], "acceptance"),
            )
        if "depends_on" in patch:
            set_task_dependencies(database, args.task_id, patch["depends_on"])
        database.execute(
            "UPDATE tasks SET title=?, description=?, priority=?, estimated_capability_level=?, "
            "capability_level=NULL, runtime_environment=?, provider_id=?, execution_policy=?, "
            "status='DRAFT', preflight_status='UNINSPECTED', preflight_execution_id=NULL, "
            "preflight_started_at=NULL, preflight_completed_at=NULL, preflight_failure=NULL, "
            "scope_hint_json=?, lock_mode=NULL, split_suggestions_json='[]', assigned_agent=NULL, "
            "heartbeat_at=NULL, completed_at=NULL, updated_at=?, progress_percent=0, "
            "progress_summary='任务定义已更新，等待重新预检。', progress_next_step='等待 Planner 原子预留。', "
            "result_summary=NULL, result_error=NULL, result_diagnostic_json=NULL, human_required=0, "
            "human_question=NULL, human_options_json='[]', human_requested_at=NULL, "
            "human_responded_at=NULL, human_response=NULL, row_version=row_version+1 WHERE id=?",
            (title.strip(), description, priority, estimated_capability_level, runtime_environment,
             provider_id, execution_policy, json_dump(scope_hint), stamp, args.task_id),
        )
        database.execute("DELETE FROM task_verifications WHERE task_id=?", (args.task_id,))
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, ?, 'DRAFT', 'task-manager', 'Operator 更新原始任务定义；必须重新预检。')",
            (args.task_id, stamp, previous_status),
        )
        revision = bump_revision(database, "task-manager")
        commit(database)
        output({"outcome": "UPDATED", "task_id": args.task_id, "status": "DRAFT",
                "preflight_status": "UNINSPECTED", "revision": revision})
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_requeue(args: argparse.Namespace) -> None:
    database = connect(args.db)
    try:
        transaction(database)
        row = database.execute("SELECT * FROM tasks WHERE id=?", (args.task_id,)).fetchone()
        if not row or row["status"] not in {
            "DRAFT",
            "NEEDS_REVIEW",
            "WAITING_HUMAN",
            "WAITING_CONFLICT",
            "SUCCEEDED",
            "FAILED",
        }:
            raise LoopError("只有草稿、待复核、等待、成功或失败任务可以重新排队")
        if row["archived_at"] is not None:
            raise LoopError("已归档任务必须先取消归档")
        require_expected_row_version(args, row["row_version"])
        if uses_preflight_schema(database) and row["preflight_status"] == "INSPECTING":
            raise LoopError("Planner 正在预检，任务不能重新排队")
        stamp = now_shanghai()
        database.execute("DELETE FROM task_conflicts WHERE task_id=?", (args.task_id,))
        diagnostic_reset = "result_diagnostic_json=NULL, " if uses_result_diagnostic_schema(database) else ""
        needs_preflight = uses_preflight_schema(database) and row["status"] in {"DRAFT", "NEEDS_REVIEW"}
        if needs_preflight:
            database.execute("DELETE FROM task_scopes WHERE task_id=?", (args.task_id,))
            replace_ordered_text(database, "task_technical_acceptance", args.task_id, [])
            replace_ordered_text(database, "task_preflight_evidence", args.task_id, [])
            database.execute(
                f"UPDATE tasks SET status='DRAFT', preflight_status='UNINSPECTED', "
                "preflight_execution_id=NULL, preflight_started_at=NULL, preflight_completed_at=NULL, "
                "preflight_failure=NULL, capability_level=NULL, lock_mode=NULL, split_suggestions_json='[]', "
                "assigned_agent=NULL, heartbeat_at=NULL, completed_at=NULL, updated_at=?, progress_percent=0, "
                "progress_summary='任务已人工送回 Planner 预检。', progress_next_step='等待 Planner 原子预留。', "
                f"{diagnostic_reset}human_required=0, human_question=NULL, human_options_json='[]', "
                "human_requested_at=NULL, human_responded_at=NULL, human_response=NULL, "
                "row_version=row_version+1 WHERE id=?",
                (stamp, args.task_id),
            )
            new_status = "DRAFT"
            next_preflight = "UNINSPECTED"
        else:
            if uses_preflight_schema(database) and row["preflight_status"] != "READY":
                raise LoopError("任务尚未 READY，不能直接重新排入 Worker 队列")
            database.execute(
                f"UPDATE tasks SET status='PENDING', assigned_agent=NULL, heartbeat_at=NULL, completed_at=NULL, "
                "updated_at=?, progress_percent=0, progress_summary='任务已人工重新排队。', "
                f"progress_next_step='等待并发 Worker 领取。', {diagnostic_reset}human_required=0, human_question=NULL, "
                "human_options_json='[]', human_requested_at=NULL, human_responded_at=NULL, human_response=NULL, "
                "row_version=row_version+1 WHERE id=?",
                (stamp, args.task_id),
            )
            new_status = "PENDING"
            next_preflight = "READY"
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, ?, ?, 'task-manager', ?)",
            (args.task_id, stamp, row["status"], new_status, args.reason or "人工重新排队。"),
        )
        revision = bump_revision(database, "task-manager")
        commit(database)
        output({"outcome": "REQUEUED", "task_id": args.task_id, "status": new_status,
                "preflight_status": next_preflight, "revision": revision})
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_cancel(args: argparse.Namespace) -> None:
    database = connect(args.db)
    try:
        transaction(database)
        row = database.execute("SELECT * FROM tasks WHERE id=?", (args.task_id,)).fetchone()
        if not row:
            raise LoopError("任务不存在")
        if row["status"] == "RUNNING":
            raise LoopError("RUNNING 任务不能取消")
        if uses_preflight_schema(database) and row["preflight_status"] == "INSPECTING":
            raise LoopError("Planner 正在预检，任务不能取消")
        if row["archived_at"] is not None:
            raise LoopError("已归档任务必须先取消归档")
        stamp = now_shanghai()
        database.execute(
            "UPDATE tasks SET status='CANCELLED', updated_at=?, progress_next_step=NULL, "
            "row_version=row_version+1 WHERE id=?",
            (stamp, args.task_id),
        )
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) VALUES(?, ?, ?, 'CANCELLED', 'task-manager', ?)",
            (args.task_id, stamp, row["status"], args.reason or "任务已取消。"),
        )
        revision = bump_revision(database, "task-manager")
        commit(database)
        output({"outcome": "CANCELLED", "task_id": args.task_id, "revision": revision})
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_validate(args: argparse.Namespace) -> None:
    database = connect(args.db)
    try:
        result = validate_database(database)
        output({"outcome": "VALID" if result["ok"] else "INVALID", **result}, 0 if result["ok"] else 1)
    finally:
        database.close()


def command_state(args: argparse.Namespace) -> None:
    database = connect(args.db)
    try:
        output(state_payload(database))
    finally:
        database.close()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Concurrent SQLite Local Agent Loop controller")
    root.add_argument("--db", default=str(DEFAULT_DB))
    commands = root.add_subparsers(dest="command", required=True)

    legacy_migrate = commands.add_parser(
        "migrate-legacy",
        help="将旧 TASKS.json 和 INBOX.json 导入 SQLite；不是空系统初始化入口",
    )
    legacy_migrate.add_argument("--tasks", required=True, help="旧 TASKS.json 的 UTF-8 路径")
    legacy_migrate.add_argument("--inbox", required=True, help="旧 INBOX.json 的 UTF-8 路径")
    legacy_migrate.add_argument("--registry", default=str(BASE_DIR.parent / "根目录清单.md"))
    legacy_migrate.add_argument("--config", default=str(CONFIG_PATH))
    legacy_migrate.add_argument("--backup-dir", default=str(BASE_DIR / "backups"))
    legacy_migrate.add_argument("--force", action="store_true")
    legacy_migrate.set_defaults(handler=command_migrate_legacy)

    migrate = commands.add_parser("migrate")
    migrate.set_defaults(handler=command_migrate)

    validate = commands.add_parser("validate")
    validate.set_defaults(handler=command_validate)
    state = commands.add_parser("state")
    state.set_defaults(handler=command_state)
    preflight_claim = commands.add_parser("preflight-claim")
    preflight_claim.add_argument("execution_id")
    preflight_claim.add_argument(
        "--runtime-environment", required=True, choices=("codex_automation",)
    )
    preflight_claim.add_argument("--sandbox", required=True, choices=("read-only",))
    preflight_claim.set_defaults(handler=command_preflight_claim)
    preflight_heartbeat = commands.add_parser("preflight-heartbeat")
    preflight_heartbeat.add_argument("execution_id")
    preflight_heartbeat.add_argument("task_id")
    preflight_heartbeat.add_argument("--expected-row-version", type=int)
    preflight_heartbeat.set_defaults(handler=command_preflight_heartbeat)
    for name, handler in (
        ("preflight-ready", command_preflight_ready),
        ("preflight-needs-review", command_preflight_needs_review),
        ("preflight-fail", command_preflight_fail),
    ):
        preflight_finish = commands.add_parser(name)
        preflight_finish.add_argument("execution_id")
        preflight_finish.add_argument("task_id")
        preflight_finish.add_argument(
            "report", nargs="?", default="-", help="UTF-8 JSON 文件；省略或使用 - 时从 stdin 读取"
        )
        preflight_finish.add_argument("--expected-row-version", type=int)
        preflight_finish.set_defaults(handler=handler)
    claim = commands.add_parser("claim")
    claim.add_argument("execution_id")
    claim_level = claim.add_mutually_exclusive_group(required=True)
    claim_level.add_argument("--profile", choices=EXECUTION_PROFILES)
    claim_level.add_argument("--capability-level", choices=CAPABILITY_LEVELS)
    claim.add_argument("--runtime-environment", required=True, choices=CLAIM_RUNTIME_ENVIRONMENTS)
    claim.add_argument("--provider-id")
    claim.add_argument("--execution-policy", choices=("automatic", "manual"))
    claim.set_defaults(handler=command_claim)
    heartbeat = commands.add_parser("heartbeat")
    heartbeat.add_argument("execution_id")
    heartbeat.add_argument("task_id")
    heartbeat.set_defaults(handler=command_heartbeat)
    extend_scope = commands.add_parser("extend-scope")
    extend_scope.add_argument("execution_id")
    extend_scope.add_argument("task_id")
    extend_scope.add_argument(
        "report", nargs="?", default="-", help="UTF-8 JSON 文件；省略或使用 - 时从 stdin 读取"
    )
    extend_scope.add_argument("--expected-row-version", type=int)
    extend_scope.set_defaults(handler=command_extend_scope)
    recover = commands.add_parser("recover")
    recover.add_argument("execution_id")
    recover.add_argument("--action", choices=("requeue", "failed", "wait"))
    recover.add_argument("--expected-row-version", type=int)
    recovery_confirmation = recover.add_mutually_exclusive_group(required=True)
    recovery_confirmation.add_argument("--runner-confirmed-terminated", action="store_true")
    recovery_confirmation.add_argument("--human-confirmed-safe", action="store_true")
    recover.set_defaults(handler=command_recover)
    finish = commands.add_parser("finish")
    finish.add_argument("execution_id")
    finish.add_argument("task_id")
    finish.add_argument("report", nargs="?", default="-", help="UTF-8 JSON 文件；省略或使用 - 时从 stdin 读取")
    finish.set_defaults(handler=command_finish)
    confirm = commands.add_parser("confirm")
    confirm.add_argument("task_id")
    confirm.add_argument("--reason")
    confirm.add_argument("--expected-row-version", type=int)
    confirm.set_defaults(handler=command_confirm)
    resolve_human = commands.add_parser("resolve-human")
    resolve_human.add_argument("task_id")
    resolve_human.add_argument("--response", required=True)
    resolve_human.add_argument("--summary")
    resolve_human.add_argument("--reason")
    resolve_human.add_argument("--expected-row-version", type=int)
    resolve_human.set_defaults(handler=command_resolve_human)
    archive = commands.add_parser("archive")
    archive.add_argument("task_id")
    archive.add_argument("--reason")
    archive.add_argument("--expected-row-version", type=int)
    archive.set_defaults(handler=command_archive)
    unarchive = commands.add_parser("unarchive")
    unarchive.add_argument("task_id")
    unarchive.add_argument("--reason")
    unarchive.set_defaults(handler=command_unarchive)
    enqueue = commands.add_parser("enqueue")
    enqueue.add_argument("file")
    enqueue.set_defaults(handler=command_enqueue)
    update = commands.add_parser("update")
    update.add_argument("task_id")
    update.add_argument("file")
    update.add_argument("--expected-row-version", type=int)
    update.set_defaults(handler=command_update)
    requeue = commands.add_parser("requeue")
    requeue.add_argument("task_id")
    requeue.add_argument("--reason")
    requeue.add_argument("--expected-row-version", type=int)
    requeue.set_defaults(handler=command_requeue)
    cancel = commands.add_parser("cancel")
    cancel.add_argument("task_id")
    cancel.add_argument("--reason")
    cancel.set_defaults(handler=command_cancel)
    return root


def main() -> None:
    args = parser().parse_args()
    try:
        args.handler(args)
    except (LoopError, sqlite3.Error, OSError, ValueError) as error:
        output({"outcome": "ERROR", "message": str(error)}, 1)


if __name__ == "__main__":
    main()
