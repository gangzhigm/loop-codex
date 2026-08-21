"""Planner 预检的领取、心跳和结果写回状态机。

Planner execution 使用独立容量，不占 Worker 名额，也不获取业务 scope 锁。预检期间
任务保持 ``DRAFT``；只有 READY 写回会在同一事务内补齐执行契约并发布为 ``PENDING``。
"""

from __future__ import annotations

import argparse
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any

from loop_agent.constants import CAPABILITY_LEVELS, LOCK_MODES
from loop_agent.control.io import (
    output,
    read_preflight_report,
    require_expected_row_version,
    validate_preflight_text_integrity,
)
from loopdb import (
    LoopError,
    bump_revision,
    commit,
    connect,
    insert_task,
    json_dump,
    load_initialization_config,
    normalize_scope,
    normalize_split_suggestions,
    normalize_string_list,
    now_shanghai,
    replace_ordered_text,
    resolve_execution_profile,
    rollback,
    set_task_dependencies,
    task_dict,
    transaction,
)


def command_schedule_preflight(args: argparse.Namespace) -> None:
    """按公共容量和优先级原子地把草稿排入 Planner 预检队列。"""
    config = load_initialization_config(args.config)
    settings = config["planner"]
    priority_levels = config["priority_policy"]["levels"]
    priority_rank = {
        priority: index for index, priority in enumerate(priority_levels)
    }
    database = connect(args.db)
    try:
        transaction(database)
        recovered = recover_timed_out_preflights(database, settings)
        active = int(
            database.execute(
                "SELECT count(*) FROM preflight_executions "
                "WHERE status IN ('QUEUED', 'INSPECTING')"
            ).fetchone()[0]
        )
        maximum = int(settings["max_active_executions"])
        available = max(0, maximum - active)
        draft_total = int(
            database.execute(
                "SELECT count(*) FROM tasks WHERE status='DRAFT'"
            ).fetchone()[0]
        )
        candidates = database.execute(
            "SELECT * FROM tasks WHERE status='DRAFT' "
            "AND preflight_status='UNINSPECTED'"
        ).fetchall()
        ordered = sorted(
            candidates,
            key=lambda task: (
                priority_rank.get(str(task["priority"]), len(priority_rank)),
                str(task["created_at"]),
                str(task["id"]),
            ),
        )
        selected = ordered[
            : min(available, int(settings["max_tasks_per_cycle"]))
        ]
        stamp = now_shanghai()
        start = datetime.fromisoformat(stamp)
        lease = (
            start + timedelta(seconds=int(settings["lease_seconds"]))
        ).isoformat(timespec="milliseconds")
        deadline = (
            start + timedelta(seconds=int(settings["attempt_timeout_seconds"]))
        ).isoformat(timespec="milliseconds")
        queued: list[dict[str, Any]] = []
        for task in selected:
            execution_id = f"planner-{uuid.uuid4()}"
            database.execute(
                "INSERT INTO preflight_executions(execution_id, task_id, status, "
                "started_at, heartbeat_at, lease_expires_at, attempt_deadline_at, "
                "claimed_task_row_version) VALUES(?, ?, 'QUEUED', ?, ?, ?, ?, ?)",
                (
                    execution_id,
                    task["id"],
                    stamp,
                    stamp,
                    lease,
                    deadline,
                    task["row_version"],
                ),
            )
            changed = database.execute(
                "UPDATE tasks SET preflight_status='QUEUED', preflight_execution_id=?, "
                "preflight_started_at=NULL, preflight_completed_at=NULL, preflight_failure=NULL, "
                "updated_at=?, progress_summary='Planner 已将任务排入预检队列。', "
                "progress_next_step='等待预检 Runner 领取。', row_version=row_version+1 "
                "WHERE id=? AND status='DRAFT' AND preflight_status='UNINSPECTED' "
                "AND row_version=?",
                (execution_id, stamp, task["id"], task["row_version"]),
            ).rowcount
            if changed != 1:
                raise LoopError("Planner 排队时任务发生并发变化")
            database.execute(
                "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
                "VALUES(?, ?, 'DRAFT', 'DRAFT', ?, 'Planner 原子地将任务排入预检队列。')",
                (task["id"], stamp, execution_id),
            )
            queued.append(
                {"task_id": str(task["id"]), "execution_id": execution_id}
            )
        revision = bump_revision(database, "scheduler-preflight")
        commit(database)
        output(
            {
                "outcome": "QUEUED" if queued else "NO_TASK",
                "execution_kind": "PLANNER",
                "draft_total": draft_total,
                "active_before": active,
                "maximum": maximum,
                "available_slots": available,
                "candidate_count": len(candidates),
                "queued_count": len(queued),
                "queued": queued,
                "recovered": recovered,
                "revision": revision,
            }
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_unschedule_preflight(args: argparse.Namespace) -> None:
    """人工撤回尚未领取的 Planner 排队 execution，并保留完整审计记录。"""
    database = connect(args.db)
    try:
        transaction(database)
        task = database.execute(
            "SELECT * FROM tasks WHERE id=?",
            (args.task_id,),
        ).fetchone()
        if task is None:
            raise LoopError("任务不存在")
        require_expected_row_version(args, task["row_version"])
        if task["status"] != "DRAFT" or task["preflight_status"] != "QUEUED":
            raise LoopError("只有尚未领取的 Planner 排队任务可以撤回")
        execution_id = task["preflight_execution_id"]
        execution = database.execute(
            "SELECT * FROM preflight_executions WHERE execution_id=? AND task_id=?",
            (execution_id, args.task_id),
        ).fetchone()
        if execution is None or execution["status"] != "QUEUED":
            raise LoopError("Planner execution 已被领取或状态不匹配")
        stamp = now_shanghai()
        reason = args.reason or "Operator 人工撤回 Planner 排队。"
        execution_changed = database.execute(
            "UPDATE preflight_executions SET status='FINISHED', finished_at=?, "
            "outcome=NULL, termination_reason=? WHERE execution_id=? AND status='QUEUED'",
            (stamp, reason, execution_id),
        ).rowcount
        task_changed = database.execute(
            "UPDATE tasks SET preflight_status='UNINSPECTED', preflight_execution_id=NULL, "
            "preflight_started_at=NULL, preflight_completed_at=NULL, preflight_failure=NULL, "
            "updated_at=?, progress_summary='Planner 排队已人工撤回。', "
            "progress_next_step='等待 Planner 再次排队。', row_version=row_version+1 "
            "WHERE id=? AND status='DRAFT' AND preflight_status='QUEUED' "
            "AND preflight_execution_id=? AND row_version=?",
            (stamp, args.task_id, execution_id, task["row_version"]),
        ).rowcount
        if execution_changed != 1 or task_changed != 1:
            raise LoopError("撤回 Planner 排队时任务发生并发变化")
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, 'DRAFT', 'DRAFT', 'task-manager', ?)",
            (args.task_id, stamp, reason),
        )
        revision = bump_revision(database, "task-manager")
        commit(database)
        output(
            {
                "outcome": "PREFLIGHT_UNSCHEDULED",
                "task_id": args.task_id,
                "execution_id": execution_id,
                "status": "DRAFT",
                "preflight_status": "UNINSPECTED",
                "row_version": int(task["row_version"]) + 1,
                "revision": revision,
            }
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def recover_timed_out_preflights(
    database: sqlite3.Connection,
    settings: dict[str, Any] | None = None,
) -> list[str]:
    """回收心跳停滞、租约过期或超过总时限的活动预检。"""
    planner_settings = settings or load_initialization_config()["planner"]
    stamp = now_shanghai()
    current = datetime.fromisoformat(stamp)
    stalled_cutoff = current - timedelta(
        seconds=int(planner_settings["stalled_after_seconds"])
    )
    recovered: list[str] = []
    executions = database.execute(
        "SELECT * FROM preflight_executions "
        "WHERE status='INSPECTING' ORDER BY started_at"
    ).fetchall()
    for execution in executions:
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
            "UPDATE preflight_executions SET status='TIMED_OUT', finished_at=?, "
            "outcome='TIMED_OUT', termination_reason=?, recovered_at=?, "
            "recovery_action='requeue' WHERE execution_id=? AND status='INSPECTING'",
            (stamp, reason, stamp, execution["execution_id"]),
        )
        changed = database.execute(
            "UPDATE tasks SET preflight_status='UNINSPECTED', "
            "preflight_execution_id=NULL, preflight_started_at=NULL, "
            "preflight_completed_at=NULL, preflight_failure=NULL, updated_at=?, "
            "progress_summary=?, progress_next_step='等待 Planner 重新预检。', "
            "row_version=row_version+1 WHERE id=? AND status='DRAFT' "
            "AND preflight_status='INSPECTING' AND preflight_execution_id=?",
            (stamp, reason, execution["task_id"], execution["execution_id"]),
        ).rowcount
        if changed:
            database.execute(
                "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
                "VALUES(?, ?, 'DRAFT', 'DRAFT', 'planner-timeout-recovery', ?)",
                (execution["task_id"], stamp, reason),
            )
        recovered.append(str(execution["execution_id"]))
    return recovered


def planner_task_payload(
    database: sqlite3.Connection, task: sqlite3.Row
) -> dict[str, Any]:
    """只向预检 Runner 暴露 Operator 定义和必要的 fencing 元数据。"""
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


def command_preflight_claim(args: argparse.Namespace) -> None:
    """让预检 Runner 原子领取 Planner 已排队的 execution。"""
    if not args.execution_id or len(args.execution_id) > 128:
        raise LoopError("Planner execution-id 无效")
    config = load_initialization_config()
    settings = config["planner"]
    boundary = settings["client_boundary"]
    if args.runtime_environment != settings["worker_runtime_environment"]:
        raise LoopError("Planner runtime_environment 与初始化配置不匹配")
    if args.sandbox != boundary["sandbox"] or args.sandbox != "read-only":
        raise LoopError("Planner 必须由 read-only sandbox 入口领取")

    database = connect(args.db)
    try:
        transaction(database)
        recovered = recover_timed_out_preflights(database)
        active = int(
            database.execute(
                "SELECT count(*) FROM preflight_executions WHERE status='INSPECTING'"
            ).fetchone()[0]
        )
        maximum = int(settings["max_active_executions"])
        task_id = getattr(args, "task_id", None)
        task = database.execute(
            "SELECT t.* FROM tasks t JOIN preflight_executions p ON p.task_id=t.id "
            "WHERE p.execution_id=? AND p.status='QUEUED' "
            "AND t.status='DRAFT' AND t.preflight_status='QUEUED' "
            "AND t.preflight_execution_id=p.execution_id "
            "AND (? IS NULL OR t.id=?)",
            (
                args.execution_id,
                task_id,
                task_id,
            ),
        ).fetchone()
        if task is None:
            commit(database)
            output(
                {
                    "outcome": "NO_TASK",
                    "execution_kind": "PLANNER",
                    "requested_task_id": task_id,
                    "active": active,
                    "maximum": maximum,
                    "recovered": recovered,
                }
            )
            return

        stamp = now_shanghai()
        start = datetime.fromisoformat(stamp)
        lease = (start + timedelta(seconds=int(settings["lease_seconds"]))).isoformat(
            timespec="milliseconds"
        )
        deadline = (
            start + timedelta(seconds=int(settings["attempt_timeout_seconds"]))
        ).isoformat(timespec="milliseconds")
        execution_changed = database.execute(
            "UPDATE preflight_executions SET status='INSPECTING', started_at=?, "
            "heartbeat_at=?, lease_expires_at=?, attempt_deadline_at=?, "
            "claimed_task_row_version=? WHERE execution_id=? AND task_id=? "
            "AND status='QUEUED'",
            (
                stamp,
                stamp,
                lease,
                deadline,
                task["row_version"],
                args.execution_id,
                task["id"],
            ),
        ).rowcount
        if execution_changed != 1:
            raise LoopError("AI 领取时 Planner execution 发生并发变化")
        changed = database.execute(
            "UPDATE tasks SET preflight_status='INSPECTING', "
            "preflight_started_at=?, preflight_completed_at=NULL, preflight_failure=NULL, "
            "updated_at=?, progress_summary=?, "
            "progress_next_step='AI 正在进行只读静态预检。', "
            "row_version=row_version+1 WHERE id=? AND status='DRAFT' "
            "AND preflight_status='QUEUED' AND preflight_execution_id=? AND row_version=?",
            (
                stamp,
                stamp,
                f"AI 已领取 Planner 排队任务 {args.execution_id}。",
                task["id"],
                args.execution_id,
                task["row_version"],
            ),
        ).rowcount
        if changed != 1:
            raise LoopError("AI 领取时任务发生并发变化")
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, 'DRAFT', 'DRAFT', ?, '预检 Runner 领取排队任务，AI 开始静态预检。')",
            (task["id"], stamp, args.execution_id),
        )
        claimed = database.execute(
            "SELECT * FROM tasks WHERE id=?", (task["id"],)
        ).fetchone()
        payload = planner_task_payload(database, claimed)
        revision = bump_revision(database, args.execution_id)
        commit(database)
        output(
            {
                "outcome": "CLAIMED",
                "execution_kind": "PLANNER",
                "execution_id": args.execution_id,
                "task_id": task["id"],
                "lease_expires_at": lease,
                "attempt_deadline_at": deadline,
                "active": active + 1,
                "maximum": maximum,
                "recovered": recovered,
                "revision": revision,
                "client_boundary": {
                    "sandbox": boundary["sandbox"],
                    "approval_policy": boundary["approval_policy"],
                    "network_access": boundary["network_access"],
                    "default_tool_action": boundary["default_tool_action"],
                    "source_access": boundary["source_access"],
                    "writeback_transport": boundary["writeback"]["transport"],
                    "allowed_writeback_commands": boundary["writeback"][
                        "allowed_commands"
                    ],
                },
                "task": payload,
            }
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_preflight_heartbeat(args: argparse.Namespace) -> None:
    """续期活动预检，同时推进任务 row_version。"""
    database = connect(args.db)
    try:
        transaction(database)
        recovered = recover_timed_out_preflights(database)
        execution = database.execute(
            "SELECT * FROM preflight_executions "
            "WHERE execution_id=? AND status='INSPECTING'",
            (args.execution_id,),
        ).fetchone()
        if execution is None or execution["task_id"] != args.task_id:
            raise LoopError("活动 Planner execution 与 task-id 不匹配或已超时")
        task = database.execute(
            "SELECT * FROM tasks WHERE id=? AND status='DRAFT' "
            "AND preflight_status='INSPECTING' AND preflight_execution_id=?",
            (args.task_id, args.execution_id),
        ).fetchone()
        if task is None:
            raise LoopError("Planner task fencing 不匹配")
        require_expected_row_version(args, task["row_version"])
        settings = load_initialization_config()["planner"]
        stamp = now_shanghai()
        lease = (
            datetime.fromisoformat(stamp)
            + timedelta(seconds=int(settings["lease_seconds"]))
        ).isoformat(timespec="milliseconds")
        database.execute(
            "UPDATE preflight_executions SET heartbeat_at=?, lease_expires_at=? "
            "WHERE execution_id=?",
            (stamp, lease, args.execution_id),
        )
        database.execute(
            "UPDATE tasks SET updated_at=?, row_version=row_version+1 WHERE id=?",
            (stamp, args.task_id),
        )
        row_version = int(task["row_version"]) + 1
        commit(database)
        output(
            {
                "outcome": "HEARTBEAT",
                "execution_kind": "PLANNER",
                "task_id": args.task_id,
                "lease_expires_at": lease,
                "row_version": row_version,
                "recovered": recovered,
            }
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def preflight_finish_context(
    database: sqlite3.Connection,
    args: argparse.Namespace,
    expected_outcome: str,
) -> tuple[sqlite3.Row | None, sqlite3.Row]:
    """校验 execution/task fencing，并允许相同结果的幂等重放。"""
    recover_timed_out_preflights(database)
    execution = database.execute(
        "SELECT * FROM preflight_executions WHERE execution_id=?",
        (args.execution_id,),
    ).fetchone()
    if execution is None or execution["task_id"] != args.task_id:
        raise LoopError("Planner execution 与 task-id 不匹配")
    if execution["status"] != "INSPECTING":
        if execution["outcome"] == expected_outcome:
            return None, execution
        raise LoopError("Planner execution 已结束，迟到结果被拒绝")
    task = database.execute(
        "SELECT * FROM tasks WHERE id=?", (args.task_id,)
    ).fetchone()
    if (
        task is None
        or task["status"] != "DRAFT"
        or task["preflight_status"] != "INSPECTING"
    ):
        raise LoopError("任务不处于 Planner INSPECTING")
    if task["preflight_execution_id"] != args.execution_id:
        raise LoopError("Planner execution fencing 不匹配")
    require_expected_row_version(args, task["row_version"])
    return task, execution


def _read_report(args: argparse.Namespace, allowed: set[str]) -> dict[str, Any]:
    report = read_preflight_report(args.report)
    if not isinstance(report, dict) or set(report) != allowed:
        raise LoopError("Planner 预检结果字段无效")
    validate_preflight_text_integrity(report)
    return report


def command_preflight_ready(args: argparse.Namespace) -> None:
    """补齐执行契约，并原子地把 DRAFT/INSPECTING 发布为 PENDING/READY。"""
    report = _read_report(
        args,
        {
            "summary",
            "capability_level",
            "scope",
            "lock_mode",
            "technical_acceptance",
            "evidence",
        },
    )
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
        report.get("technical_acceptance"),
        "technical_acceptance",
        allow_empty=False,
    )
    evidence = normalize_string_list(
        report.get("evidence"), "evidence", allow_empty=False
    )
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
            output(
                {
                    "outcome": "ALREADY_FINISHED",
                    "execution_kind": "PLANNER",
                    "task_id": args.task_id,
                    "preflight_status": "READY",
                }
            )
            return
        resolve_execution_profile(
            task["runtime_environment"], task["provider_id"], capability
        )
        stamp = now_shanghai()
        database.execute("DELETE FROM task_scopes WHERE task_id=?", (args.task_id,))
        database.executemany(
            "INSERT INTO task_scopes(task_id, ordinal, scope, scope_key) "
            "VALUES(?, ?, ?, ?)",
            [
                (args.task_id, index, item["scope"], item["scope_key"])
                for index, item in enumerate(normalized_scopes)
            ],
        )
        replace_ordered_text(
            database, "task_technical_acceptance", args.task_id, technical
        )
        replace_ordered_text(
            database, "task_preflight_evidence", args.task_id, evidence
        )
        database.execute(
            "UPDATE tasks SET status='PENDING', preflight_status='READY', "
            "preflight_execution_id=NULL, preflight_completed_at=?, "
            "preflight_failure=NULL, capability_level=?, lock_mode=?, "
            "split_suggestions_json='[]', updated_at=?, progress_percent=0, "
            "progress_summary=?, progress_next_step='等待匹配的 Worker 领取。', "
            "human_required=0, human_question=NULL, human_options_json='[]', "
            "human_requested_at=NULL, human_responded_at=NULL, human_response=NULL, "
            "row_version=row_version+1 WHERE id=?",
            (
                stamp,
                capability,
                lock_mode,
                stamp,
                summary.strip(),
                args.task_id,
            ),
        )
        database.execute(
            "UPDATE preflight_executions SET status='FINISHED', finished_at=?, "
            "outcome='READY' WHERE execution_id=? AND status='INSPECTING'",
            (stamp, args.execution_id),
        )
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, 'DRAFT', 'PENDING', ?, ?)",
            (args.task_id, stamp, args.execution_id, summary.strip()),
        )
        revision = bump_revision(database, args.execution_id)
        commit(database)
        output(
            {
                "outcome": "READY",
                "execution_kind": "PLANNER",
                "task_id": args.task_id,
                "status": "PENDING",
                "preflight_status": "READY",
                "revision": revision,
            }
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_preflight_split(args: argparse.Namespace) -> None:
    """原子创建 Planner 拆分出的草稿，改写依赖并逻辑取消父任务。"""
    report = _read_report(args, {"summary", "split_suggestions", "evidence"})
    summary = report.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise LoopError("SPLIT 预检 summary 不能为空")
    evidence = normalize_string_list(
        report.get("evidence"), "evidence", allow_empty=False
    )
    suggestions = normalize_split_suggestions(report.get("split_suggestions"))
    if len(suggestions) != 1 or len(suggestions[0]["tasks"]) < 2:
        raise LoopError("SPLIT 必须提供唯一方案且至少包含两个子任务")

    database = connect(args.db)
    try:
        transaction(database)
        task, _ = preflight_finish_context(database, args, "SPLIT")
        if task is None:
            commit(database)
            output(
                {
                    "outcome": "ALREADY_FINISHED",
                    "execution_kind": "PLANNER",
                    "task_id": args.task_id,
                    "preflight_status": "FAILED",
                }
            )
            return
        if database.execute(
            "SELECT 1 FROM task_attachments WHERE task_id=? LIMIT 1",
            (args.task_id,),
        ).fetchone():
            raise LoopError("带附件任务无法自动确定附件归属，必须返回 NEEDS_REVIEW")

        plan = suggestions[0]
        proposed_tasks = plan["tasks"]
        child_ids = [item["id"] for item in proposed_tasks]
        child_set = set(child_ids)
        if args.task_id in child_set:
            raise LoopError("拆分子任务不能复用父任务 id")
        existing = database.execute(
            f"SELECT id FROM tasks WHERE id IN ({','.join('?' for _ in child_ids)})",
            child_ids,
        ).fetchall()
        if existing:
            raise LoopError(
                "拆分子任务 id 已存在: " + ", ".join(str(row[0]) for row in existing)
            )

        for proposed in proposed_tasks:
            parallel = set(proposed["parallel_with"])
            if proposed["id"] in parallel or not parallel.issubset(child_set):
                raise LoopError("parallel_with 只能引用其他拆分子任务")
            if args.task_id in proposed["depends_on"]:
                raise LoopError("拆分子任务不能依赖被替代的父任务")

        parent_dependencies = [
            str(row[0])
            for row in database.execute(
                "SELECT dependency_id FROM task_dependencies WHERE task_id=? ORDER BY dependency_id",
                (args.task_id,),
            ).fetchall()
        ]
        downstream_ids = [
            str(row[0])
            for row in database.execute(
                "SELECT task_id FROM task_dependencies WHERE dependency_id=? ORDER BY task_id",
                (args.task_id,),
            ).fetchall()
        ]
        stamp = now_shanghai()
        acceptance_by_child = {item["id"]: item["acceptance"] for item in proposed_tasks}
        for proposed in proposed_tasks:
            insert_task(
                database,
                {
                    "id": proposed["id"],
                    "title": proposed["title"],
                    "description": proposed["description"],
                    "status": "DRAFT",
                    "priority": task["priority"],
                    "estimated_capability_level": proposed["capability_level"],
                    "runtime_environment": task["runtime_environment"],
                    "provider_id": task["provider_id"],
                    "execution_policy": task["execution_policy"],
                    "scope_hint": proposed["scope"],
                    "acceptance": acceptance_by_child[proposed["id"]],
                    "created_at": stamp,
                    "progress": {
                        "percent": 0,
                        "summary": f"由 Planner 从 {args.task_id} 自动拆分。",
                        "next_step": "等待 Planner 为子任务形成执行契约。",
                    },
                },
                actor=args.execution_id,
            )

        for proposed in proposed_tasks:
            dependencies = list(
                dict.fromkeys([*parent_dependencies, *proposed["depends_on"]])
            )
            set_task_dependencies(database, proposed["id"], dependencies)

        for downstream_id in downstream_ids:
            dependencies = [
                str(row[0])
                for row in database.execute(
                    "SELECT dependency_id FROM task_dependencies WHERE task_id=? ORDER BY dependency_id",
                    (downstream_id,),
                ).fetchall()
                if str(row[0]) != args.task_id
            ]
            set_task_dependencies(
                database,
                downstream_id,
                list(dict.fromkeys([*dependencies, *child_ids])),
            )

        replace_ordered_text(
            database, "task_preflight_evidence", args.task_id, evidence
        )
        database.execute(
            "UPDATE tasks SET status='CANCELLED', preflight_status='FAILED', "
            "preflight_execution_id=NULL, preflight_completed_at=?, preflight_failure=NULL, "
            "capability_level=NULL, lock_mode=NULL, split_suggestions_json=?, updated_at=?, "
            "progress_percent=0, progress_summary=?, progress_next_step=NULL, "
            "human_required=0, human_question=NULL, human_options_json='[]', "
            "human_requested_at=NULL, human_responded_at=NULL, human_response=NULL, "
            "row_version=row_version+1 WHERE id=?",
            (
                stamp,
                json_dump(suggestions),
                stamp,
                summary.strip(),
                args.task_id,
            ),
        )
        database.execute(
            "UPDATE preflight_executions SET status='FINISHED', finished_at=?, "
            "outcome='SPLIT', termination_reason=? WHERE execution_id=? AND status='INSPECTING'",
            (stamp, plan["reason"], args.execution_id),
        )
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, 'DRAFT', 'CANCELLED', ?, ?)",
            (args.task_id, stamp, args.execution_id, summary.strip()),
        )
        revision = bump_revision(database, args.execution_id)
        commit(database)
        output(
            {
                "outcome": "SPLIT",
                "execution_kind": "PLANNER",
                "task_id": args.task_id,
                "status": "CANCELLED",
                "child_task_ids": child_ids,
                "revision": revision,
            }
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_preflight_needs_review(args: argparse.Namespace) -> None:
    """保存人工问题和拆分建议，并将任务转为 NEEDS_REVIEW/FAILED。"""
    report = _read_report(
        args,
        {"summary", "question", "options", "split_suggestions", "evidence"},
    )
    summary = report.get("summary")
    question = report.get("question")
    if not isinstance(summary, str) or not summary.strip():
        raise LoopError("NEEDS_REVIEW summary 不能为空")
    if not isinstance(question, str) or not question.strip():
        raise LoopError("NEEDS_REVIEW question 不能为空")
    options = normalize_string_list(report.get("options"), "options")
    suggestions = normalize_split_suggestions(report.get("split_suggestions"))
    evidence = normalize_string_list(
        report.get("evidence"), "evidence", allow_empty=False
    )
    database = connect(args.db)
    try:
        transaction(database)
        task, _ = preflight_finish_context(database, args, "NEEDS_REVIEW")
        if task is None:
            commit(database)
            output(
                {
                    "outcome": "ALREADY_FINISHED",
                    "execution_kind": "PLANNER",
                    "task_id": args.task_id,
                    "preflight_status": "FAILED",
                }
            )
            return
        stamp = now_shanghai()
        database.execute("DELETE FROM task_scopes WHERE task_id=?", (args.task_id,))
        replace_ordered_text(database, "task_technical_acceptance", args.task_id, [])
        replace_ordered_text(
            database, "task_preflight_evidence", args.task_id, evidence
        )
        database.execute(
            "UPDATE tasks SET status='NEEDS_REVIEW', preflight_status='FAILED', "
            "preflight_execution_id=NULL, preflight_completed_at=?, preflight_failure=?, "
            "capability_level=NULL, lock_mode=NULL, split_suggestions_json=?, updated_at=?, "
            "progress_percent=0, progress_summary=?, "
            "progress_next_step='等待 Operator 取得人工决定。', human_required=1, "
            "human_question=?, human_options_json=?, human_requested_at=?, "
            "human_responded_at=NULL, human_response=NULL, row_version=row_version+1 WHERE id=?",
            (
                stamp,
                summary.strip(),
                json_dump(suggestions),
                stamp,
                summary.strip(),
                question.strip(),
                json_dump(options),
                stamp,
                args.task_id,
            ),
        )
        database.execute(
            "UPDATE preflight_executions SET status='FINISHED', finished_at=?, "
            "outcome='NEEDS_REVIEW' WHERE execution_id=? AND status='INSPECTING'",
            (stamp, args.execution_id),
        )
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, 'DRAFT', 'NEEDS_REVIEW', ?, ?)",
            (args.task_id, stamp, args.execution_id, summary.strip()),
        )
        revision = bump_revision(database, args.execution_id)
        commit(database)
        output(
            {
                "outcome": "NEEDS_REVIEW",
                "execution_kind": "PLANNER",
                "task_id": args.task_id,
                "status": "NEEDS_REVIEW",
                "preflight_status": "FAILED",
                "revision": revision,
            }
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_preflight_fail(args: argparse.Namespace) -> None:
    """记录预检技术失败，并将任务交回 Operator。"""
    report = _read_report(args, {"summary", "error", "evidence"})
    summary = report.get("summary")
    error = report.get("error")
    if (
        not isinstance(summary, str)
        or not summary.strip()
        or not isinstance(error, str)
        or not error.strip()
    ):
        raise LoopError("FAILED 预检 summary/error 不能为空")
    evidence = normalize_string_list(
        report.get("evidence"), "evidence", allow_empty=False
    )
    database = connect(args.db)
    try:
        transaction(database)
        task, _ = preflight_finish_context(database, args, "FAILED")
        if task is None:
            commit(database)
            output(
                {
                    "outcome": "ALREADY_FINISHED",
                    "execution_kind": "PLANNER",
                    "task_id": args.task_id,
                    "preflight_status": "FAILED",
                }
            )
            return
        stamp = now_shanghai()
        database.execute("DELETE FROM task_scopes WHERE task_id=?", (args.task_id,))
        replace_ordered_text(database, "task_technical_acceptance", args.task_id, [])
        replace_ordered_text(
            database, "task_preflight_evidence", args.task_id, evidence
        )
        database.execute(
            "UPDATE tasks SET status='NEEDS_REVIEW', preflight_status='FAILED', "
            "preflight_execution_id=NULL, preflight_completed_at=?, preflight_failure=?, "
            "capability_level=NULL, lock_mode=NULL, split_suggestions_json='[]', updated_at=?, "
            "progress_percent=0, progress_summary=?, "
            "progress_next_step='等待 Operator 修正后重新预检。', human_required=1, "
            "human_question='Planner 预检失败；是否修正任务定义后重新预检？', "
            "human_options_json='[\"修正后重新预检\",\"取消任务\"]', human_requested_at=?, "
            "human_responded_at=NULL, human_response=NULL, row_version=row_version+1 WHERE id=?",
            (
                stamp,
                error.strip(),
                stamp,
                summary.strip(),
                stamp,
                args.task_id,
            ),
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
        output(
            {
                "outcome": "FAILED",
                "execution_kind": "PLANNER",
                "task_id": args.task_id,
                "status": "NEEDS_REVIEW",
                "preflight_status": "FAILED",
                "revision": revision,
            }
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()
