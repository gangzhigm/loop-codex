"""Worker execution lifecycle: claim, heartbeat, scope extension, and finish.

The four commands in this module form one fenced execution protocol:

``claim``
    Atomically chooses a compatible task, creates an execution, and acquires
    every declared scope lock.
``heartbeat``
    Renews both the execution lease and its scope locks.
``extend-scope``
    Acquires additional locks before a Worker touches newly discovered files.
``finish``
    Writes the final report and releases locks owned by that execution.

Every mutating command verifies both ``execution_id`` and ``task_id``. This is
the fencing boundary that prevents a stale Worker from updating a task after a
replacement execution has claimed it. Recovery of a lost execution lives in
``control.recovery`` because its locks must be quarantined, not normally
released as part of this lifecycle.
"""

from __future__ import annotations

# 中文排查：Worker 的 claim、heartbeat、扩展 scope 和 finish 组成一套带 fencing 的执行协议。
# 无法领取先查容量、依赖和 scope 冲突；迟到写回先查 execution_id、task_id 与锁凭证。
# 任何锁释放和终态写入必须属于同一 execution，禁止为方便排障跳过凭证校验。

import argparse
import sqlite3
from typing import Any

from loop_agent.control.io import output, read_json_source, require_expected_row_version
from loop_agent.control.queue import dependencies_ready, requeue_resolved_conflicts
from loop_agent.control.recovery import (
    recovery_required_records,
    stalled_executions,
    transition_recovery_states,
)
from loopdb import (
    FINAL_EXECUTION_STATUSES,
    LEGACY_PROFILE_TO_CAPABILITY,
    LOCK_MODES,
    LoopError,
    bump_revision,
    commit,
    connect,
    execution_setting,
    expires_at,
    global_parallel_limit,
    json_dump,
    legacy_profile_for,
    normalize_execution_target,
    normalize_result_diagnostic,
    normalize_scope,
    normalize_string_list,
    now_shanghai,
    platform_parallel_limit,
    replace_ordered_text,
    resolve_execution_profile,
    rollback,
    scope_conflicts_for_keys,
    task_dict,
    transaction,
    uses_capability_schema,
    uses_preflight_schema,
    uses_recovery_schema,
    uses_result_diagnostic_schema,
)


def task_scopes_and_conflicts(
    database: sqlite3.Connection, task_id: str
) -> tuple[list[sqlite3.Row], list[dict[str, Any]]]:
    """Load a task's canonical scope keys and project active lock conflicts."""
    scopes = database.execute(
        "SELECT DISTINCT scope_key FROM task_scopes WHERE task_id=? ORDER BY scope_key",
        (task_id,),
    ).fetchall()
    return scopes, scope_conflicts_for_keys(
        database, [scope["scope_key"] for scope in scopes]
    )


def describe_conflicting_task(
    task_id: str,
    conflicts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Keep conflict output stable for Dashboard and automation consumers."""
    return {"task_id": task_id, "conflicts": conflicts}


def scope_lock_credential(
    database: sqlite3.Connection,
    execution_id: str,
    task_id: str,
) -> dict[str, Any]:
    """Return the lock credential a Worker must retain while it is active.

    Schema probes keep this projection readable during database migration. A
    credential contains only lock identity and lease metadata; it is not a
    bearer secret and does not grant authority without execution fencing.
    """
    task_columns = {row[1] for row in database.execute("PRAGMA table_info(tasks)")}
    task = (
        database.execute("SELECT lock_mode FROM tasks WHERE id=?", (task_id,)).fetchone()
        if "lock_mode" in task_columns
        else None
    )
    lock_columns = {
        row[1] for row in database.execute("PRAGMA table_info(scope_locks)")
    }
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
    """Normalize legacy profile flags into the current execution dimensions."""
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
    snapshot = resolve_execution_profile(
        runtime_environment, provider_id, capability_level
    )
    return {
        **snapshot,
        "execution_policy": execution_policy,
        "execution_profile": legacy_profile_for(capability_level, execution_policy),
        "requested_runtime_environment": args.runtime_environment,
    }


def command_claim(args: argparse.Namespace) -> None:
    """Atomically claim the highest-priority compatible, unblocked task.

    Selection happens inside one immediate transaction. A candidate is skipped
    when dependencies are incomplete or any scope key conflicts. Scope-conflict
    tasks stay ``PENDING`` so another concurrent profile can continue selecting
    unrelated work instead of treating the queue as globally blocked.
    """
    if not args.execution_id or len(args.execution_id) > 128:
        raise LoopError("execution-id 无效")
    target = claim_target(args)
    database = connect(args.db)
    try:
        transaction(database)
        capability_schema = uses_capability_schema(database)
        preflight_schema = uses_preflight_schema(database)
        if database.execute(
            "SELECT 1 FROM executions WHERE execution_id=?", (args.execution_id,)
        ).fetchone():
            raise LoopError("execution-id 已存在")

        # Reconcile stale executions before counting capacity. Recovery removes
        # them from active slots but retains quarantined locks until confirmed.
        transition_recovery_states(database, stalled_executions(database))
        recovery_required = recovery_required_records(database)
        compatible_recoveries = [
            item
            for item in recovery_required
            if item["runtime_environment"] == target["runtime_environment"]
            and item["provider_id"] == target["provider_id"]
            and item["capability_level"] == target["capability_level"]
            and item["execution_policy"] == target["execution_policy"]
        ]
        requeued = requeue_resolved_conflicts(database)
        active = database.execute(
            "SELECT count(*) FROM executions WHERE status='RUNNING'"
        ).fetchone()[0]
        maximum = global_parallel_limit()
        if active >= maximum:
            commit(database)
            output(
                {
                    "outcome": "SLOT_FULL",
                    "limit_scope": "global",
                    "profile": target["execution_profile"],
                    "capability_level": target["capability_level"],
                    "runtime_environment": target["runtime_environment"],
                    "provider_id": target["provider_id"],
                    "active": active,
                    "maximum": maximum,
                    "recovery_required": compatible_recoveries,
                }
            )
            return
        if capability_schema:
            platform_active = database.execute(
                "SELECT count(*) FROM executions WHERE status='RUNNING' AND runtime_environment=?",
                (target["runtime_environment"],),
            ).fetchone()[0]
        else:
            legacy_environment = (
                "deepseek"
                if target["runtime_environment"] == "self_hosted_agent"
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
            output(
                {
                    "outcome": "SLOT_FULL",
                    "limit_scope": "platform",
                    "profile": target["execution_profile"],
                    "capability_level": target["capability_level"],
                    "runtime_environment": target["runtime_environment"],
                    "provider_id": target["provider_id"],
                    "active": active,
                    "maximum": maximum,
                    "platform_active": platform_active,
                    "platform_maximum": platform_maximum,
                    "recovery_required": compatible_recoveries,
                }
            )
            return

        # New schemas require Planner evidence before a task enters the Worker
        # queue. Compatibility branches remain for inspecting/upgrading older DBs.
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
                    target["runtime_environment"],
                    target["provider_id"],
                    target["capability_level"],
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
                    target["runtime_environment"],
                    target["provider_id"],
                    target["capability_level"],
                    target["execution_policy"],
                ),
            ).fetchall()
        else:
            if target["execution_policy"] == "manual" and target["capability_level"] != "L5":
                raise LoopError("Schema 3.3.0 兼容层无法表示 L1-L4 manual execution_policy")
            legacy_environment = (
                "deepseek"
                if target["runtime_environment"] == "self_hosted_agent"
                else target["runtime_environment"]
            )
            if (
                target["runtime_environment"] == "self_hosted_agent"
                and target["provider_id"] != "deepseek"
            ):
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
            candidate_scopes, conflicts = task_scopes_and_conflicts(
                database, candidate["id"]
            )
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
                if compatible_recoveries
                and target["runtime_environment"] == "codex_automation"
                else "NO_TASK"
            )
            commit(database)
            output(
                {
                    "outcome": outcome,
                    "profile": target["execution_profile"],
                    "capability_level": target["capability_level"],
                    "runtime_environment": target["runtime_environment"],
                    "provider_id": target["provider_id"],
                    "active": active,
                    "recovery_required": compatible_recoveries,
                    "requeued": requeued,
                }
            )
            return

        # Execution, locks, and task state are created in the same transaction;
        # no Worker can observe a RUNNING task without its corresponding locks.
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
                    args.execution_id,
                    task_row["id"],
                    stamp,
                    stamp,
                    expiry,
                    target["runtime_environment"],
                    target["provider_id"],
                    target["capability_level"],
                    target["execution_policy"],
                    target["model"],
                    target["reasoning"],
                    target["attempt_timeout_seconds"],
                    target["max_retries"],
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
                (
                    scope["scope_key"],
                    task_row["id"],
                    args.execution_id,
                    stamp,
                    expiry,
                ),
            )
        diagnostic_reset = (
            "result_diagnostic_json=NULL, "
            if uses_result_diagnostic_schema(database)
            else ""
        )
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
        database.execute(
            "DELETE FROM task_verifications WHERE task_id=?", (task_row["id"],)
        )
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, 'PENDING', 'RUNNING', ?, '并发 Worker 原子领取任务并获取 scope 锁。')",
            (task_row["id"], stamp, args.execution_id),
        )
        revision = bump_revision(database, args.execution_id)
        claimed = database.execute(
            "SELECT * FROM tasks WHERE id=?", (task_row["id"],)
        ).fetchone()
        payload = task_dict(database, claimed)
        lock_credential = scope_lock_credential(
            database, args.execution_id, task_row["id"]
        )
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
    """Renew an active execution and all of its active scope-lock leases."""
    database = connect(args.db)
    try:
        transaction(database)
        execution = database.execute(
            "SELECT * FROM executions WHERE execution_id=? AND status='RUNNING'",
            (args.execution_id,),
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
        output(
            {
                "outcome": "HEARTBEAT",
                "task_id": args.task_id,
                "lease_expires_at": expiry,
                "scope_lock_credential": credential,
            }
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_extend_scope(args: argparse.Namespace) -> None:
    """Atomically add normalized scopes and locks before new files are touched."""
    report = read_json_source(args.report)
    if not isinstance(report, dict) or set(report) != {"scope"}:
        raise LoopError("scope 扩展结果必须只包含 scope")
    requested_scopes = normalize_string_list(
        report.get("scope"), "scope", allow_empty=False
    )
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
        normalized = [
            normalize_scope(scope, task["lock_mode"]) for scope in requested_scopes
        ]
        canonical = [item["scope"].casefold() for item in normalized]
        if len(canonical) != len(set(canonical)):
            raise LoopError("scope 扩展项规范化后不能重复")
        existing_rows = database.execute(
            "SELECT scope, scope_key FROM task_scopes WHERE task_id=? ORDER BY ordinal",
            (args.task_id,),
        ).fetchall()
        existing_scopes = {row["scope"].casefold() for row in existing_rows}
        additions = [
            item
            for item in normalized
            if item["scope"].casefold() not in existing_scopes
        ]
        new_keys = sorted({item["scope_key"] for item in additions})
        conflicts = scope_conflicts_for_keys(
            database, new_keys, exclude_execution_id=args.execution_id
        )
        if conflicts:
            credential = scope_lock_credential(
                database, args.execution_id, args.task_id
            )
            commit(database)
            output(
                {
                    "outcome": "SCOPE_EXTENSION_CONFLICT",
                    "task_id": args.task_id,
                    "execution_id": args.execution_id,
                    "conflicts": conflicts,
                    "scope_lock_credential": credential,
                }
            )
            return
        if not additions:
            credential = scope_lock_credential(
                database, args.execution_id, args.task_id
            )
            commit(database)
            output(
                {
                    "outcome": "SCOPE_ALREADY_REGISTERED",
                    "task_id": args.task_id,
                    "execution_id": args.execution_id,
                    "scope_lock_credential": credential,
                }
            )
            return
        stamp = now_shanghai()
        next_ordinal = int(
            database.execute(
                "SELECT COALESCE(max(ordinal), -1) + 1 FROM task_scopes WHERE task_id=?",
                (args.task_id,),
            ).fetchone()[0]
        )
        database.executemany(
            "INSERT INTO task_scopes(task_id, ordinal, scope, scope_key) VALUES(?, ?, ?, ?)",
            [
                (
                    args.task_id,
                    next_ordinal + index,
                    item["scope"],
                    item["scope_key"],
                )
                for index, item in enumerate(additions)
            ],
        )
        held_keys = {
            row[0]
            for row in database.execute(
                "SELECT scope_key FROM scope_locks WHERE execution_id=?",
                (args.execution_id,),
            ).fetchall()
        }
        expiry = execution["lease_expires_at"]
        database.executemany(
            "INSERT INTO scope_locks(scope_key, task_id, execution_id, acquired_at, lease_expires_at) "
            "VALUES(?, ?, ?, ?, ?)",
            [
                (scope_key, args.task_id, args.execution_id, stamp, expiry)
                for scope_key in new_keys
                if scope_key not in held_keys
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
        output(
            {
                "outcome": "SCOPE_EXTENDED",
                "task_id": args.task_id,
                "execution_id": args.execution_id,
                "added_scope": [item["scope"] for item in additions],
                "scope_lock_credential": credential,
                "revision": revision,
            }
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_finish(args: argparse.Namespace) -> None:
    """Persist a validated Worker report and close its active execution."""
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
            "SELECT * FROM executions WHERE execution_id=? AND status='RUNNING'",
            (args.execution_id,),
        ).fetchone()
        if not execution or execution["task_id"] != args.task_id:
            raise LoopError("活动 execution 与 task-id 不匹配")
        task = database.execute(
            "SELECT status FROM tasks WHERE id=?", (args.task_id,)
        ).fetchone()
        if not task or task["status"] != "RUNNING":
            raise LoopError("任务不处于 RUNNING")
        stamp = now_shanghai()
        waiting = status == "WAITING_HUMAN"
        diagnostic_column = (
            "result_diagnostic_json=?, "
            if uses_result_diagnostic_schema(database)
            else ""
        )
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
        values.extend(
            [
                int(waiting),
                report.get("question") if waiting else None,
                json_dump(report.get("options") or []),
                stamp if waiting else None,
                args.task_id,
            ]
        )
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
            replace_ordered_text(
                database,
                "task_completed_items",
                args.task_id,
                report.get("completed") or [],
            )
        replace_ordered_text(
            database,
            "task_verifications",
            args.task_id,
            report.get("verification") or [],
        )
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, 'RUNNING', ?, ?, ?)",
            (
                args.task_id,
                stamp,
                status,
                args.execution_id,
                report.get("reason") or report["summary"],
            ),
        )
        database.execute(
            "UPDATE executions SET status='FINISHED', finished_at=?, outcome=? WHERE execution_id=?",
            (stamp, status, args.execution_id),
        )
        database.execute(
            "DELETE FROM scope_locks WHERE execution_id=?", (args.execution_id,)
        )
        revision = bump_revision(database, args.execution_id)
        commit(database)
        output(
            {
                "outcome": "FINISHED",
                "task_id": args.task_id,
                "status": status,
                "requeued_conflicts": [],
                "revision": revision,
            }
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()
