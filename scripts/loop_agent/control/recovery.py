"""Recover executions whose heartbeat, lease, or attempt timeout has expired.

Recovery deliberately separates two concerns that are easy to conflate while
debugging the queue:

1. An execution that stops proving liveness is removed from active capacity.
2. Its scope locks remain quarantined until the old process or Codex session is
   known to be unable to write again.

That separation prevents a replacement Worker from entering the same scope
while an abandoned process may still be modifying files. Codex client sessions
require a human safety confirmation because the local service cannot terminate
them. Controlled runners instead require process-termination confirmation from
the runner. This module owns those state transitions; claim code only asks it
to refresh recovery state before selecting new work.
"""

from __future__ import annotations

# 中文排查：失联检测、execution 隔离、QUARANTINED 锁和人工安全恢复都在本模块。
# 恢复异常按 heartbeat、lease、attempt timeout、execution 状态和锁状态的顺序核对。
# 时间信号只能证明会话可能失联，不能证明旧写入者已结束；释放隔离锁必须有终止确认。

import argparse
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from loop_agent.control.io import output, require_expected_row_version
from loopdb import (
    LoopError,
    bump_revision,
    commit,
    connect,
    execution_setting,
    now_shanghai,
    rollback,
    transaction,
    uses_capability_schema,
    uses_recovery_schema,
    uses_result_diagnostic_schema,
)


def stalled_executions(database: sqlite3.Connection) -> list[dict[str, Any]]:
    """Project executions that no longer satisfy the configured liveness rules.

    This function does not mutate the database. Keeping detection read-only
    makes it useful to both claim-time housekeeping and diagnostic commands.
    Older schema versions are still readable so migrations can inspect a
    pre-upgrade database without failing on newer columns.
    """
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
            + (
                "termination_reason, recovery_required "
                if recovery_schema
                else "NULL AS termination_reason, 0 AS recovery_required "
            )
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
        if not (
            heartbeat_stalled
            or lease_expired
            or attempt_timed_out
            or execution["recovery_required"]
        ):
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
    """Return the machine code and operator-facing label for a liveness loss."""
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
    """Move newly stale executions and their tasks into recoverable states.

    Scope locks become ``QUARANTINED`` rather than being deleted. The task moves
    from ``RUNNING`` to ``WAITING_HUMAN`` so capacity is released without
    claiming that the old writer is safe. A later timeout may upgrade a
    previously stalled execution to ``TIMED_OUT`` while preserving quarantine.
    """
    if not uses_recovery_schema(database):
        return
    stamp = now_shanghai()
    for recovery in recoveries:
        old_status = recovery["execution_status"]
        if old_status == "TIMED_OUT" or (
            old_status == "STALLED" and not recovery["attempt_timed_out"]
        ):
            continue
        new_status = "TIMED_OUT" if recovery["attempt_timed_out"] else "STALLED"
        reason_code, reason_label = _recovery_reason(recovery)
        outcome = (
            "INFRASTRUCTURE_TIMEOUT"
            if new_status == "TIMED_OUT"
            else "RECOVERY_REQUIRED"
        )
        updated = database.execute(
            "UPDATE executions SET status=?, finished_at=?, outcome=?, termination_reason=?, "
            "recovery_required=1 WHERE execution_id=? AND status=?",
            (
                new_status,
                stamp,
                outcome,
                reason_code,
                recovery["execution_id"],
                old_status,
            ),
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
                (
                    stamp,
                    f"execution 已到达 attempt timeout；scope 继续隔离。原因：{reason_label}。",
                    recovery["task_id"],
                ),
            )
            database.execute(
                "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
                "VALUES(?, ?, 'WAITING_HUMAN', 'WAITING_HUMAN', 'execution-timeout-detector', ?)",
                (
                    recovery["task_id"],
                    stamp,
                    f"execution 转为 TIMED_OUT；scope 继续隔离。原因：{reason_label}。",
                ),
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
                json.dumps(
                    ["确认结束并重新排队", "确认结束并标记失败", "继续等待"],
                    ensure_ascii=False,
                ),
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
                    recovery["task_id"],
                    stamp,
                    f"execution 转为 {new_status} 并释放活动容量；scope 转为 QUARANTINED。原因：{reason_label}。",
                ),
            )


def recovery_required_records(database: sqlite3.Connection) -> list[dict[str, Any]]:
    """Group quarantined scope rows into one operator record per execution."""
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
                "heartbeat_stalled": "HEARTBEAT_STALLED"
                in (row["termination_reason"] or ""),
                "lease_expired": "LEASE_EXPIRED"
                in (row["termination_reason"] or ""),
                "attempt_timed_out": "ATTEMPT_TIMED_OUT"
                in (row["termination_reason"] or ""),
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
    """Release a quarantined execution after its old writer is confirmed safe."""
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
            if execution["recovery_action"] and (
                args.action is None or execution["recovery_action"] == args.action
            ):
                commit(database)
                output(
                    {
                        "outcome": "ALREADY_RECOVERED",
                        "execution_id": args.execution_id,
                        "task_id": execution["task_id"],
                        "recovery_action": execution["recovery_action"],
                    }
                )
                return
            raise LoopError("execution 不处于待恢复状态")
        candidates = {
            item["execution_id"]: item
            for item in recovery_required_records(database)
        }
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
        if (
            not task
            or task["status"] != "WAITING_HUMAN"
            or task["assigned_agent"] != args.execution_id
        ):
            raise LoopError("待恢复任务状态或 execution fencing 不匹配")
        require_expected_row_version(args, task["row_version"])
        stamp = now_shanghai()
        action = args.action
        if action is None:
            action = (
                "requeue"
                if task["attempt"] < int(execution_setting("max_attempts", 2))
                else "failed"
            )
        if action == "wait":
            if (
                execution["recovery_action"] == "wait"
                and task["human_response"] == "continue_waiting"
            ):
                commit(database)
                output(
                    {
                        "outcome": "ALREADY_WAITING",
                        "execution_id": args.execution_id,
                        "task_id": execution["task_id"],
                        "task_status": "WAITING_HUMAN",
                        "scope_status": "QUARANTINED",
                    }
                )
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
            output(
                {
                    "outcome": "WAITING",
                    "execution_id": args.execution_id,
                    "task_id": execution["task_id"],
                    "task_status": "WAITING_HUMAN",
                    "scope_status": "QUARANTINED",
                    "revision": revision,
                }
            )
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
        diagnostic_reset = (
            "result_diagnostic_json=NULL, "
            if uses_result_diagnostic_schema(database)
            else ""
        )
        database.execute(
            f"UPDATE tasks SET status=?, assigned_agent=NULL, heartbeat_at=NULL, updated_at=?, "
            "completed_at=?, progress_percent=?, progress_summary=?, progress_next_step=?, result_error=?, "
            f"{diagnostic_reset}human_required=0, human_question=NULL, "
            "human_options_json='[]', human_requested_at=NULL, "
            "human_responded_at=?, human_response=?, row_version=row_version+1 WHERE id=?",
            (
                new_status,
                stamp,
                stamp if new_status == "FAILED" else None,
                100 if new_status == "FAILED" else 0,
                summary,
                "等待下一次兼容执行器领取。" if new_status == "PENDING" else None,
                summary if new_status == "FAILED" else None,
                stamp,
                action,
                execution["task_id"],
            ),
        )
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, 'WAITING_HUMAN', ?, ?, ?)",
            (execution["task_id"], stamp, new_status, actor, summary),
        )
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
                "requeued_conflicts": [],
                "revision": revision,
            }
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()
