"""Worker 的领取、心跳、scope 扩展与结束状态机。

四个命令共同组成一套带 fencing 的执行协议：

``claim``
    在一个事务中接管 Dispatcher 已创建的兼容 QUEUED execution、取得全部已声明 scope 锁
    并转为 RUNNING；
``heartbeat``
    同时续期 execution 租约和该 execution 持有的活动 scope 锁；
``extend-scope``
    Worker 接触预检范围外的新文件前，先原子登记 scope 并获取新增锁；
``finish``
    校验最终报告，结束 execution，并只释放该 execution 拥有的锁。

所有写命令都同时核对 ``execution_id`` 和 ``task_id``，部分入口还核对 row_version。
这是阻止旧 Worker 在替代 execution 领取后继续写回的 fencing 边界。丢失 execution
的恢复位于 ``control.recovery``，因为其锁必须先隔离确认，不能走正常 finish 直接删除。
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
    LOCK_MODES,
    LoopError,
    bump_revision,
    commit,
    connect,
    execution_setting,
    expires_at,
    global_parallel_limit,
    json_dump,
    load_initialization_config,
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
)


def task_scopes_and_conflicts(
    database: sqlite3.Connection, task_id: str
) -> tuple[list[sqlite3.Row], list[dict[str, Any]]]:
    """读取任务去重后的规范 scope_key，并计算当前活动锁冲突投影。"""
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
    """把单个候选任务的冲突包装成 Dashboard 与 Runner 共用的稳定结构。"""
    return {"task_id": task_id, "conflicts": conflicts}


def scope_lock_credential(
    database: sqlite3.Connection,
    execution_id: str,
    task_id: str,
) -> dict[str, Any]:
    """返回 Worker 活动期间必须保留并核对的 scope 锁凭证。

    凭证仅包含 execution/task 身份、锁模式、scope_key、状态和租约元数据；它不是
    授权密钥，脱离数据库中的 execution fencing 不能单独获得写权限。
    """
    task = database.execute(
        "SELECT lock_mode FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    locks = database.execute(
        "SELECT scope_key, status, acquired_at, lease_expires_at FROM scope_locks "
        "WHERE execution_id=? AND task_id=? ORDER BY scope_key",
        (execution_id, task_id),
    ).fetchall()
    return {
        "execution_id": execution_id,
        "task_id": task_id,
        "lock_mode": task["lock_mode"] if task is not None else None,
        "scope_keys": [row["scope_key"] for row in locks],
        "locks": [dict(row) for row in locks],
    }


def claim_target(args: argparse.Namespace) -> dict[str, Any]:
    """解析当前环境、Provider、能力等级和策略的固定执行快照。"""
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
        "requested_runtime_environment": args.runtime_environment,
    }


def command_claim(args: argparse.Namespace) -> None:
    """原子领取最高优先级且兼容、依赖完成、无 scope 冲突的任务。

    选择发生在立即事务中。领取前先把停滞 execution 转入恢复状态，再检查全局和平台
    容量。候选依赖未完成或 scope 冲突时只跳过该任务，冲突任务继续保持 ``PENDING``，
    让同档 Worker 仍可选择后续无关任务。选中后 execution、全部锁和任务 RUNNING
    状态在同一事务创建，任何一步失败都会整体回滚。
    """
    if not args.execution_id or len(args.execution_id) > 128:
        raise LoopError("execution-id 无效")
    target = claim_target(args)
    database = connect(args.db)
    try:
        transaction(database)
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
        queued_execution = database.execute(
            "SELECT * FROM executions WHERE execution_id=?",
            (args.execution_id,),
        ).fetchone()

        if queued_execution is not None:
            if queued_execution["execution_kind"] != "WORKER":
                raise LoopError("execution-kind 与 Worker 入口不匹配")
            if queued_execution["status"] != "QUEUED":
                raise LoopError("execution-id 已存在且不可领取")
            route_fields = (
                "runtime_environment",
                "provider_id",
                "capability_level",
                "execution_policy",
            )
            if any(queued_execution[field] != target[field] for field in route_fields):
                raise LoopError("Worker 启动档位与 QUEUED execution 快照不匹配")

            active = int(
                database.execute(
                    "SELECT count(*) FROM executions WHERE status='RUNNING'"
                ).fetchone()[0]
            )
            maximum = global_parallel_limit()
            platform_active = int(
                database.execute(
                    "SELECT count(*) FROM executions WHERE status='RUNNING' "
                    "AND runtime_environment=?",
                    (target["runtime_environment"],),
                ).fetchone()[0]
            )
            platform_maximum = platform_parallel_limit(target["runtime_environment"])
            if active >= maximum or platform_active >= platform_maximum:
                commit(database)
                output(
                    {
                        "outcome": "SLOT_FULL",
                        "limit_scope": "global" if active >= maximum else "platform",
                        "execution_id": args.execution_id,
                        "active": active,
                        "maximum": maximum,
                        "platform_active": platform_active,
                        "platform_maximum": platform_maximum,
                        "recovery_required": compatible_recoveries,
                        "requeued": requeued,
                    }
                )
                return
            task_row = database.execute(
                "SELECT * FROM tasks WHERE id=? AND status='QUEUED' "
                "AND preflight_status='READY' AND runtime_environment=? "
                "AND provider_id IS ? AND capability_level=? AND execution_policy=?",
                (
                    queued_execution["task_id"],
                    target["runtime_environment"],
                    target["provider_id"],
                    target["capability_level"],
                    target["execution_policy"],
                ),
            ).fetchone()
            if task_row is None:
                raise LoopError("QUEUED execution 与任务状态或路由不匹配")
            if not dependencies_ready(database, task_row["id"]):
                commit(database)
                output(
                    {
                        "outcome": "NO_TASK",
                        "execution_id": args.execution_id,
                        "reason": "DEPENDENCY_NOT_READY",
                        "recovery_required": compatible_recoveries,
                        "requeued": requeued,
                    }
                )
                return
            scopes, conflicts = task_scopes_and_conflicts(database, task_row["id"])
            if conflicts:
                commit(database)
                output(
                    {
                        "outcome": "CONFLICT",
                        "execution_id": args.execution_id,
                        "task_id": task_row["id"],
                        "conflicts": conflicts,
                        "recovery_required": compatible_recoveries,
                        "requeued": requeued,
                    }
                )
                return
            stamp = now_shanghai()
            expiry = expires_at(int(execution_setting("task_lease_seconds", 3600)))
            changed_execution = database.execute(
                "UPDATE executions SET status='RUNNING', heartbeat_at=?, lease_expires_at=? "
                "WHERE execution_id=? AND status='QUEUED'",
                (stamp, expiry, args.execution_id),
            ).rowcount
            if changed_execution != 1:
                raise LoopError("领取 QUEUED execution 时发生并发变化")
            for scope in scopes:
                database.execute(
                    "INSERT INTO scope_locks(scope_key, task_id, execution_id, acquired_at, lease_expires_at) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (scope["scope_key"], task_row["id"], args.execution_id, stamp, expiry),
                )
            changed_task = database.execute(
                "UPDATE tasks SET status='RUNNING', assigned_agent=?, started_at=?, updated_at=?, "
                "heartbeat_at=?, completed_at=NULL, attempt=attempt+1, progress_summary=?, "
                "progress_next_step=?, result_summary=NULL, result_error=NULL, "
                "result_diagnostic_json=NULL, human_required=0, human_question=NULL, "
                "human_options_json='[]', human_requested_at=NULL, human_responded_at=NULL, "
                "human_response=NULL, row_version=row_version+1 "
                "WHERE id=? AND status='QUEUED' AND row_version=?",
                (
                    args.execution_id,
                    stamp,
                    stamp,
                    stamp,
                    f"Runner Worker {args.execution_id} 已领取排队任务。",
                    "在当前 Worker execution 中执行并验证。",
                    task_row["id"],
                    task_row["row_version"],
                ),
            ).rowcount
            if changed_task != 1:
                raise LoopError("领取 QUEUED 任务时发生并发变化")
            database.execute(
                "DELETE FROM task_verifications WHERE task_id=?", (task_row["id"],)
            )
            database.execute(
                "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
                "VALUES(?, ?, 'QUEUED', 'RUNNING', ?, "
                "'Runner Worker 原子领取已排队 execution 并获取 scope 锁。')",
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
            commit(database)
            output(
                {
                    "outcome": "CLAIMED",
                    "execution_id": args.execution_id,
                    "execution_kind": "WORKER",
                    "lease_expires_at": expiry,
                    "active": active + 1,
                    "maximum": maximum,
                    "capability_level": target["capability_level"],
                    "execution_policy": target["execution_policy"],
                    "runtime_environment": target["runtime_environment"],
                    "provider_id": target["provider_id"],
                    "execution_profile": {
                        "runtime_environment": queued_execution["runtime_environment"],
                        "provider_id": queued_execution["provider_id"],
                        "capability_level": queued_execution["capability_level"],
                        "model": queued_execution["model"],
                        "reasoning": queued_execution["reasoning"],
                        "attempt_timeout_seconds": queued_execution["attempt_timeout_seconds"],
                        "max_retries": queued_execution["max_retries"],
                    },
                    "platform_active": platform_active + 1,
                    "platform_maximum": platform_maximum,
                    "revision": revision,
                    "scope_lock_credential": lock_credential,
                    "recovery_required": compatible_recoveries,
                    "requeued": requeued,
                    "task": payload,
                }
            )
            return

        if not load_initialization_config()["runner"]["allow_legacy_direct_claim"]:
            commit(database)
            output(
                {
                    "outcome": "NO_TASK",
                    "execution_id": args.execution_id,
                    "reason": "EXECUTION_NOT_QUEUED",
                    "recovery_required": compatible_recoveries,
                    "requeued": requeued,
                }
            )
            return

        # 统计容量前先转换停滞 execution。恢复流程会释放它占用的活动名额，但旧锁仍
        # 保持隔离，直到恢复确认完成，不能因为“进程不在”就直接删除。
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
                    "capability_level": target["capability_level"],
                    "runtime_environment": target["runtime_environment"],
                    "provider_id": target["provider_id"],
                    "active": active,
                    "maximum": maximum,
                    "recovery_required": compatible_recoveries,
                }
            )
            return
        platform_active = database.execute(
            "SELECT count(*) FROM executions WHERE status='RUNNING' AND runtime_environment=?",
            (target["runtime_environment"],),
        ).fetchone()[0]
        platform_maximum = platform_parallel_limit(target["runtime_environment"])
        if platform_active >= platform_maximum:
            commit(database)
            output(
                {
                    "outcome": "SLOT_FULL",
                    "limit_scope": "platform",
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

        # 只有带完整 Planner 契约的 READY 任务可以进入 Worker 队列。
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
            commit(database)
            output(
                {
                    "outcome": "NO_TASK",
                    "capability_level": target["capability_level"],
                    "runtime_environment": target["runtime_environment"],
                    "provider_id": target["provider_id"],
                    "active": active,
                    "recovery_required": compatible_recoveries,
                    "requeued": requeued,
                }
            )
            return

        # execution、scope 锁和任务 RUNNING 状态必须在同一事务建立，保证任何 Worker
        # 都不会观察到“任务已运行但尚未持锁”的中间状态。
        lease_seconds = int(execution_setting("task_lease_seconds", 3600))
        expiry = expires_at(lease_seconds)
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
        database.execute(
            f"UPDATE tasks SET status='RUNNING', assigned_agent=?, started_at=?, updated_at=?, heartbeat_at=?, "
            "completed_at=NULL, attempt=attempt+1, progress_summary=?, progress_next_step=?, "
            "result_summary=NULL, result_error=NULL, result_diagnostic_json=NULL, "
            "human_required=0, human_question=NULL, "
            "human_options_json='[]', human_requested_at=NULL, human_responded_at=NULL, human_response=NULL, "
            "row_version=row_version+1 WHERE id=?",
            (
                args.execution_id,
                stamp,
                stamp,
                stamp,
                f"并发执行 {args.execution_id} 已领取任务。",
                "在当前 Runner execution 中执行并验证。",
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
        commit(database)
        output(
            {
                "outcome": "CLAIMED",
                "execution_id": args.execution_id,
                "lease_expires_at": expiry,
                "active": active + 1,
                "maximum": maximum,
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
    """续期活动 execution 及其全部活动 scope 锁。

    execution 必须仍为 RUNNING 且 task_id 匹配。任务心跳和 row_version 同步更新，
    返回最新锁凭证，供 Runner 继续操作前确认租约范围。
    """
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
        database.execute(
            "UPDATE scope_locks SET lease_expires_at=? "
            "WHERE execution_id=? AND status='ACTIVE'",
            (expiry, args.execution_id),
        )
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
    """Worker 接触新文件前，原子扩展规范化 scope 与对应锁。

    报告只能包含 scope。命令核对活动 execution、任务归属、锁模式和 row_version，
    再排除已登记项并检查新增 scope_key 冲突。冲突时不写入任何范围；无新增项时幂等
    返回；成功时 scope、锁、进度和历史在同一事务提交。
    """
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
    """保存经过校验的 Worker 最终报告并关闭其活动 execution。

    SUCCEEDED 必须有验证记录，FAILED 必须有错误，WAITING_HUMAN 必须有问题；成功
    报告不得携带失败诊断。只有匹配的 RUNNING execution/task 可以结束。事务内更新
    任务终态、完成项、验证、历史和 execution，最后只删除该 execution 的 scope 锁。
    """
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
            diagnostic_json,
        ]
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
              result_summary=?, result_error=?, result_diagnostic_json=?,
              human_required=?, human_question=?,
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
