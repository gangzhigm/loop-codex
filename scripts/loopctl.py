from __future__ import annotations

import argparse
import json
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
    CONFIG_PATH,
    DEFAULT_DB,
    DEPENDENCY_COMPLETE_STATUSES,
    EXECUTION_PROFILES,
    FINAL_EXECUTION_STATUSES,
    PRIORITIES,
    LoopError,
    all_tasks,
    bump_revision,
    commit,
    connect,
    configured_projects,
    execution_setting,
    expires_at,
    initialize_schema,
    insert_task,
    json_dump,
    load_initialization_config,
    migrate_schema,
    now_shanghai,
    parse_project_registry,
    profile_parallel_limit,
    replace_ordered_text,
    resolve_scope_key,
    rollback,
    set_task_dependencies,
    state_payload,
    task_dict,
    task_exists,
    transaction,
    validate_database,
)


LEGACY_STATUS_MAP = {
    "CLAIMED": "WAITING_HUMAN",
    "BLOCKED": "WAITING_HUMAN",
    "STALLED": "WAITING_HUMAN",
}


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


def backup_legacy(tasks_path: Path, inbox_path: Path, backup_root: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    destination = backup_root / f"sqlite-migration-{stamp}"
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copy2(tasks_path, destination / tasks_path.name)
    shutil.copy2(inbox_path, destination / inbox_path.name)
    return destination


def normalize_legacy_task(task: dict[str, Any]) -> dict[str, Any]:
    normalized = json.loads(json.dumps(task, ensure_ascii=False))
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


def command_init(args: argparse.Namespace) -> None:
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
                "outcome": "INITIALIZED",
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


def recover_expired(database: sqlite3.Connection) -> list[dict[str, str]]:
    stamp = now_shanghai()
    stalled_cutoff = (
        datetime.fromisoformat(stamp)
        - timedelta(seconds=int(execution_setting("stalled_after_seconds", 300)))
    ).isoformat(timespec="milliseconds")
    expired = database.execute(
        "SELECT execution_id, task_id, heartbeat_at, lease_expires_at FROM executions "
        "WHERE status='RUNNING' AND (lease_expires_at<=? OR heartbeat_at<=?)",
        (stamp, stalled_cutoff),
    ).fetchall()
    recovered: list[dict[str, str]] = []
    max_attempts = int(execution_setting("max_attempts", 2))
    for execution in expired:
        heartbeat_stalled = execution["heartbeat_at"] <= stalled_cutoff
        recovery_outcome = "HEARTBEAT_STALLED" if heartbeat_stalled else "LEASE_EXPIRED"
        recovery_reason = "执行心跳超时" if heartbeat_stalled else "执行租约过期"
        task = database.execute("SELECT status, attempt FROM tasks WHERE id=?", (execution["task_id"],)).fetchone()
        database.execute(
            "UPDATE executions SET status='EXPIRED', finished_at=?, outcome=? WHERE execution_id=?",
            (stamp, recovery_outcome, execution["execution_id"]),
        )
        database.execute("DELETE FROM scope_locks WHERE execution_id=?", (execution["execution_id"],))
        if not task or task["status"] != "RUNNING":
            continue
        new_status = "PENDING" if task["attempt"] < max_attempts else "FAILED"
        database.execute(
            "UPDATE tasks SET status=?, assigned_agent=NULL, heartbeat_at=NULL, updated_at=?, "
            "completed_at=CASE WHEN ?='FAILED' THEN ? ELSE NULL END, "
            "progress_percent=CASE WHEN ?='FAILED' THEN 100 ELSE 0 END, "
            "progress_summary=?, progress_next_step=?, result_error=?, row_version=row_version+1 WHERE id=?",
            (
                new_status,
                stamp,
                new_status,
                stamp,
                new_status,
                f"{recovery_reason}，任务已恢复。" if new_status == "PENDING" else f"{recovery_reason}且达到最大尝试次数。",
                "等待下一次领取。" if new_status == "PENDING" else None,
                None if new_status == "PENDING" else f"{recovery_reason}且达到最大尝试次数。",
                execution["task_id"],
            ),
        )
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) VALUES(?, ?, 'RUNNING', ?, 'lease-recovery', ?)",
            (execution["task_id"], stamp, new_status, f"{recovery_reason}，按最大尝试次数恢复。"),
        )
        recovered.append(
            {"task_id": execution["task_id"], "status": new_status, "outcome": recovery_outcome}
        )
    return recovered


def requeue_resolved_conflicts(database: sqlite3.Connection) -> list[str]:
    rows = database.execute("SELECT id FROM tasks WHERE status='WAITING_CONFLICT'").fetchall()
    stamp = now_shanghai()
    requeued: list[str] = []
    for row in rows:
        active = database.execute(
            "SELECT 1 FROM task_conflicts c JOIN executions e ON e.execution_id=c.blocker_execution_id "
            "WHERE c.task_id=? AND e.status='RUNNING' LIMIT 1",
            (row["id"],),
        ).fetchone()
        if active:
            continue
        database.execute("DELETE FROM task_conflicts WHERE task_id=?", (row["id"],))
        database.execute(
            "UPDATE tasks SET status='PENDING', updated_at=?, progress_summary='scope 冲突已解除，任务重新排队。', "
            "progress_next_step='等待并发 Worker 领取。', row_version=row_version+1 WHERE id=?",
            (stamp, row["id"]),
        )
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, 'WAITING_CONFLICT', 'PENDING', 'conflict-recovery', '阻塞执行已结束，scope 冲突解除。')",
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


def task_scopes_and_conflicts(
    database: sqlite3.Connection, task_id: str
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    scopes = database.execute(
        "SELECT DISTINCT scope_key FROM task_scopes WHERE task_id=? ORDER BY scope_key",
        (task_id,),
    ).fetchall()
    conflicts = []
    for scope in scopes:
        lock = database.execute(
            "SELECT scope_key, task_id, execution_id FROM scope_locks WHERE scope_key=?",
            (scope["scope_key"],),
        ).fetchone()
        if lock:
            conflicts.append(lock)
    return scopes, conflicts


def defer_conflicting_task(
    database: sqlite3.Connection,
    task_id: str,
    conflicts: list[sqlite3.Row],
    stamp: str,
) -> dict[str, Any]:
    database.execute(
        "UPDATE tasks SET status='WAITING_CONFLICT', updated_at=?, "
        "progress_summary='检测到正在执行任务的 scope 冲突。', "
        "progress_next_step='等待阻塞任务结束后自动重新排队。', row_version=row_version+1 WHERE id=?",
        (stamp, task_id),
    )
    for lock in conflicts:
        database.execute(
            "INSERT OR REPLACE INTO task_conflicts(task_id, scope_key, blocker_task_id, blocker_execution_id, detected_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (task_id, lock["scope_key"], lock["task_id"], lock["execution_id"], stamp),
        )
    database.execute(
        "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
        "VALUES(?, ?, 'PENDING', 'WAITING_CONFLICT', 'concurrent-claimer', '检测到 project scope 锁冲突。')",
        (task_id, stamp),
    )
    return {"task_id": task_id, "conflicts": [dict(item) for item in conflicts]}


def command_claim(args: argparse.Namespace) -> None:
    if not args.execution_id or len(args.execution_id) > 128:
        raise LoopError("execution-id 无效")
    database = connect(args.db)
    try:
        transaction(database)
        recovered = recover_expired(database)
        requeued = requeue_resolved_conflicts(database)
        active = database.execute("SELECT count(*) FROM executions WHERE status='RUNNING'").fetchone()[0]
        maximum = int(execution_setting("max_parallel_tasks", 6))
        if active >= maximum:
            commit(database)
            output({
                "outcome": "SLOT_FULL", "limit_scope": "global", "profile": args.profile,
                "active": active, "maximum": maximum, "recovered": recovered,
            })
            return
        profile_active = database.execute(
            "SELECT count(*) FROM executions e JOIN tasks t ON t.id=e.task_id "
            "WHERE e.status='RUNNING' AND t.execution_profile=?",
            (args.profile,),
        ).fetchone()[0]
        profile_maximum = profile_parallel_limit(args.profile)
        if profile_active >= profile_maximum:
            commit(database)
            output({
                "outcome": "SLOT_FULL", "limit_scope": "profile", "profile": args.profile,
                "active": active, "maximum": maximum, "profile_active": profile_active,
                "profile_maximum": profile_maximum, "recovered": recovered,
            })
            return
        if database.execute("SELECT 1 FROM executions WHERE execution_id=?", (args.execution_id,)).fetchone():
            raise LoopError("execution-id 已存在")
        candidates = database.execute(
            "SELECT * FROM tasks WHERE status='PENDING' AND execution_profile=? ORDER BY "
            "CASE priority WHEN 'blocker' THEN 0 WHEN 'critical' THEN 1 WHEN 'high' THEN 2 "
            "WHEN 'medium' THEN 3 ELSE 4 END, created_at, id",
            (args.profile,),
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
                    defer_conflicting_task(database, candidate["id"], conflicts, stamp)
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
                        "task_id": deferred_conflicts[0]["task_id"],
                        "conflicts": deferred_conflicts[0]["conflicts"],
                        "deferred_conflicts": deferred_conflicts,
                        "recovered": recovered,
                        "requeued": requeued,
                        "revision": revision,
                    }
                )
                return
            commit(database)
            output({
                "outcome": "NO_TASK", "profile": args.profile, "active": active,
                "recovered": recovered, "requeued": requeued,
            })
            return
        lease_seconds = int(execution_setting("task_lease_seconds", 3600))
        expiry = expires_at(lease_seconds)
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
        database.execute(
            "UPDATE tasks SET status='RUNNING', assigned_agent=?, started_at=?, updated_at=?, heartbeat_at=?, "
            "completed_at=NULL, attempt=attempt+1, progress_summary=?, progress_next_step=?, "
            "result_summary=NULL, result_error=NULL, human_required=0, human_question=NULL, "
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
        commit(database)
        output(
            {
                "outcome": "CLAIMED",
                "execution_id": args.execution_id,
                "lease_expires_at": expiry,
                "active": active + 1,
                "maximum": maximum,
                "profile": args.profile,
                "profile_active": profile_active + 1,
                "profile_maximum": profile_maximum,
                "revision": revision,
                "deferred_conflicts": deferred_conflicts,
                "recovered": recovered,
                "requeued": requeued,
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
        database.execute(
            "UPDATE scope_locks SET lease_expires_at=? WHERE execution_id=?", (expiry, args.execution_id)
        )
        database.execute(
            "UPDATE tasks SET heartbeat_at=?, updated_at=?, row_version=row_version+1 WHERE id=? AND status='RUNNING'",
            (stamp, stamp, args.task_id),
        )
        commit(database)
        output({"outcome": "HEARTBEAT", "task_id": args.task_id, "lease_expires_at": expiry})
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
        database.execute(
            """
            UPDATE tasks SET status=?, updated_at=?, heartbeat_at=?, completed_at=?,
              progress_percent=?, progress_summary=?, progress_next_step=?,
              result_summary=?, result_error=?, human_required=?, human_question=?,
              human_options_json=?, human_requested_at=?, human_responded_at=NULL,
              human_response=NULL, row_version=row_version+1
            WHERE id=?
            """,
            (
                status,
                stamp,
                stamp,
                None if waiting else stamp,
                max(0, min(99, int(report.get("percent", 0)))) if waiting else 100,
                report["summary"],
                report.get("next_step", "等待人工答复。") if waiting else None,
                report["summary"] if status == "SUCCEEDED" else None,
                report.get("error") if status == "FAILED" else None,
                int(waiting),
                report.get("question") if waiting else None,
                json_dump(report.get("options") or []),
                stamp if waiting else None,
                args.task_id,
            ),
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
        requeued = requeue_resolved_conflicts(database)
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
        task = database.execute("SELECT status, archived_at FROM tasks WHERE id=?", (args.task_id,)).fetchone()
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
        output({"outcome": "CONFIRMED", "task_id": args.task_id, "revision": revision})
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
            "SELECT status, archived_at FROM tasks WHERE id=?", (args.task_id,)
        ).fetchone()
        if not task:
            raise LoopError("任务不存在")
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
                    "revision": bump_revision(database, "task-manager"),
                }
            )
            return
        stamp = now_shanghai()
        reason = args.reason or "人工归档任务。"
        database.execute("UPDATE tasks SET archived_at=? WHERE id=?", (stamp, args.task_id))
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
            requested_status = item.get("status", "PENDING")
            if requested_status not in {"DRAFT", "PENDING"}:
                raise LoopError("新任务 status 仅支持 DRAFT 或 PENDING")
            task = {
                **item,
                "status": requested_status,
                "created_at": item.get("created_at", stamp),
                "updated_at": stamp,
                "progress": item.get("progress")
                or {
                    "percent": 0,
                    "summary": "任务已进入 SQLite 队列。",
                    "completed": [],
                    "next_step": "等待并发 Worker 领取。",
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
    allowed = {"title", "description", "priority", "execution_profile"}
    unknown = set(patch) - allowed - {"scope", "depends_on", "acceptance"}
    if unknown:
        raise LoopError("不支持的更新字段: " + ", ".join(sorted(unknown)))
    database = connect(args.db)
    try:
        transaction(database)
        task = database.execute("SELECT status, archived_at FROM tasks WHERE id=?", (args.task_id,)).fetchone()
        if not task:
            raise LoopError("任务不存在")
        if task["status"] == "RUNNING":
            raise LoopError("RUNNING 任务不能修改")
        if task["archived_at"] is not None:
            raise LoopError("已归档任务必须先取消归档")
        if "priority" in patch and patch["priority"] not in PRIORITIES:
            raise LoopError(f"任务优先级无效: {patch['priority']}")
        if "execution_profile" in patch and patch["execution_profile"] not in EXECUTION_PROFILES:
            raise LoopError(f"执行档位无效: {patch['execution_profile']}")
        stamp = now_shanghai()
        assignments: list[str] = []
        values: list[Any] = []
        for field in ("title", "description", "priority", "execution_profile"):
            if field in patch:
                assignments.append(f"{field}=?")
                values.append(patch[field])
        if assignments:
            assignments.extend(["updated_at=?", "row_version=row_version+1"])
            values.extend([stamp, args.task_id])
            database.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE id=?", values)
        if "scope" in patch:
            database.execute("DELETE FROM task_scopes WHERE task_id=?", (args.task_id,))
            for index, scope in enumerate(patch["scope"]):
                database.execute(
                    "INSERT INTO task_scopes(task_id, ordinal, scope, scope_key) VALUES(?, ?, ?, ?)",
                    (args.task_id, index, scope, resolve_scope_key(scope)),
                )
        if "acceptance" in patch:
            replace_ordered_text(database, "task_acceptance", args.task_id, patch["acceptance"])
        if "depends_on" in patch:
            set_task_dependencies(database, args.task_id, patch["depends_on"])
        if not assignments:
            database.execute(
                "UPDATE tasks SET updated_at=?, row_version=row_version+1 WHERE id=?",
                (stamp, args.task_id),
            )
        revision = bump_revision(database, "task-manager")
        commit(database)
        output({"outcome": "UPDATED", "task_id": args.task_id, "revision": revision})
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_requeue(args: argparse.Namespace) -> None:
    database = connect(args.db)
    try:
        transaction(database)
        row = database.execute("SELECT status, archived_at FROM tasks WHERE id=?", (args.task_id,)).fetchone()
        if not row or row["status"] not in {
            "DRAFT",
            "WAITING_HUMAN",
            "WAITING_CONFLICT",
            "SUCCEEDED",
            "FAILED",
        }:
            raise LoopError("只有草稿、等待、成功或失败任务可以重新排队")
        if row["archived_at"] is not None:
            raise LoopError("已归档任务必须先取消归档")
        stamp = now_shanghai()
        database.execute("DELETE FROM task_conflicts WHERE task_id=?", (args.task_id,))
        database.execute(
            "UPDATE tasks SET status='PENDING', assigned_agent=NULL, heartbeat_at=NULL, completed_at=NULL, "
            "updated_at=?, progress_percent=0, progress_summary='任务已人工重新排队。', "
            "progress_next_step='等待并发 Worker 领取。', human_required=0, human_question=NULL, "
            "human_options_json='[]', human_requested_at=NULL, human_responded_at=NULL, human_response=NULL, "
            "row_version=row_version+1 WHERE id=?",
            (stamp, args.task_id),
        )
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) VALUES(?, ?, ?, 'PENDING', 'task-manager', ?)",
            (args.task_id, stamp, row["status"], args.reason or "人工重新排队。"),
        )
        revision = bump_revision(database, "task-manager")
        commit(database)
        output({"outcome": "REQUEUED", "task_id": args.task_id, "revision": revision})
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_cancel(args: argparse.Namespace) -> None:
    database = connect(args.db)
    try:
        transaction(database)
        row = database.execute("SELECT status, archived_at FROM tasks WHERE id=?", (args.task_id,)).fetchone()
        if not row:
            raise LoopError("任务不存在")
        if row["status"] == "RUNNING":
            raise LoopError("RUNNING 任务不能取消")
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

    init = commands.add_parser("init")
    init.add_argument("--tasks", required=True, help="旧 TASKS.json 的 UTF-8 路径")
    init.add_argument("--inbox", required=True, help="旧 INBOX.json 的 UTF-8 路径")
    init.add_argument("--registry", default=str(BASE_DIR.parent / "根目录清单.md"))
    init.add_argument("--config", default=str(CONFIG_PATH))
    init.add_argument("--backup-dir", default=str(BASE_DIR / "backups"))
    init.add_argument("--force", action="store_true")
    init.set_defaults(handler=command_init)

    migrate = commands.add_parser("migrate")
    migrate.set_defaults(handler=command_migrate)

    validate = commands.add_parser("validate")
    validate.set_defaults(handler=command_validate)
    state = commands.add_parser("state")
    state.set_defaults(handler=command_state)
    claim = commands.add_parser("claim")
    claim.add_argument("execution_id")
    claim.add_argument("--profile", required=True, choices=EXECUTION_PROFILES)
    claim.set_defaults(handler=command_claim)
    heartbeat = commands.add_parser("heartbeat")
    heartbeat.add_argument("execution_id")
    heartbeat.add_argument("task_id")
    heartbeat.set_defaults(handler=command_heartbeat)
    finish = commands.add_parser("finish")
    finish.add_argument("execution_id")
    finish.add_argument("task_id")
    finish.add_argument("report", nargs="?", default="-", help="UTF-8 JSON 文件；省略或使用 - 时从 stdin 读取")
    finish.set_defaults(handler=command_finish)
    confirm = commands.add_parser("confirm")
    confirm.add_argument("task_id")
    confirm.add_argument("--reason")
    confirm.set_defaults(handler=command_confirm)
    archive = commands.add_parser("archive")
    archive.add_argument("task_id")
    archive.add_argument("--reason")
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
    update.set_defaults(handler=command_update)
    requeue = commands.add_parser("requeue")
    requeue.add_argument("task_id")
    requeue.add_argument("--reason")
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
