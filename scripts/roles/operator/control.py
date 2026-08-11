"""Human-facing task management commands used by the Operator and Dashboard.

These commands edit task definitions or perform explicit human workflow
transitions. They never claim Worker work. New and materially edited tasks are
returned to ``DRAFT / UNINSPECTED`` so the Planner must rebuild technical
scope, acceptance checks, evidence, and capability classification.

Most commands accept ``--expected-row-version``. That optimistic concurrency
check is important for browser actions: it prevents an old page from silently
overwriting changes made by an automation or another Operator session.
"""

from __future__ import annotations

# 中文排查：Operator 的确认、人工答复、归档、创建、更新、重排和取消都从本文件进入。
# 每个命令先校验状态与 row_version，再在单个事务中写历史；并发错误优先检查这两道门禁。
# 本角色只管理任务事实，不领取任务，也不实现任务描述中的业务代码。

import argparse
from pathlib import Path

from loop_agent.control.io import output, read_json, require_expected_row_version
from loopdb import (
    ARCHIVABLE_STATUSES,
    CAPABILITY_LEVELS,
    EXECUTION_PROFILES,
    LEGACY_PROFILE_TO_CAPABILITY,
    PRIORITIES,
    LoopError,
    bump_revision,
    commit,
    configured_projects,
    connect,
    insert_task,
    json_dump,
    legacy_profile_for,
    normalize_execution_target,
    normalize_string_list,
    now_shanghai,
    replace_ordered_text,
    resolve_execution_profile,
    resolve_scope_key,
    rollback,
    set_task_dependencies,
    task_dict,
    transaction,
    uses_preflight_schema,
    uses_result_diagnostic_schema,
)


def command_confirm(args: argparse.Namespace) -> None:
    """Record that a human reviewer accepted a successful task."""
    database = connect(args.db)
    try:
        transaction(database)
        task = database.execute(
            "SELECT status, archived_at, row_version FROM tasks WHERE id=?",
            (args.task_id,),
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
    """Complete a Worker-blocked task after the final human answer is known."""
    response = str(args.response or "").strip()
    if not response:
        raise LoopError("人工答复不能为空")
    database = connect(args.db)
    try:
        transaction(database)
        task = database.execute(
            "SELECT * FROM tasks WHERE id=?", (args.task_id,)
        ).fetchone()
        if not task:
            raise LoopError("任务不存在")
        require_expected_row_version(args, task["row_version"])
        if task["archived_at"] is not None:
            raise LoopError("已归档任务必须先取消归档")
        if task["status"] not in {"WAITING_HUMAN", "PENDING"}:
            raise LoopError("只有等待人工的任务可以由人工答复直接完成")
        if database.execute(
            "SELECT 1 FROM executions WHERE task_id=? AND status='RUNNING'",
            (args.task_id,),
        ).fetchone():
            raise LoopError("任务存在活动 execution，不能由人工答复直接完成")
        if database.execute(
            "SELECT 1 FROM scope_locks WHERE task_id=?", (args.task_id,)
        ).fetchone():
            raise LoopError("任务仍持有 scope 锁，不能由人工答复直接完成")

        # PENDING is accepted only for the narrow case where a WAITING_HUMAN
        # task was accidentally requeued and has not been claimed again.
        if task["status"] == "PENDING":
            latest_history = database.execute(
                "SELECT from_status, to_status FROM task_history WHERE task_id=? ORDER BY id DESC LIMIT 1",
                (args.task_id,),
            ).fetchone()
            if not latest_history or tuple(latest_history) != (
                "WAITING_HUMAN",
                "PENDING",
            ):
                raise LoopError(
                    "PENDING 任务仅可在刚从 WAITING_HUMAN 误重排且尚未再次领取时直接完成"
                )
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
        diagnostic_reset = (
            "result_diagnostic_json=NULL, "
            if uses_result_diagnostic_schema(database)
            else ""
        )
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
        revision = bump_revision(database, "human-resolution")
        commit(database)
        output(
            {
                "outcome": "HUMAN_RESOLVED",
                "task_id": args.task_id,
                "status": "SUCCEEDED",
                "row_version": task["row_version"] + 1,
                "requeued_conflicts": [],
                "revision": revision,
            }
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_archive(args: argparse.Namespace) -> None:
    """Set ``archived_at`` without overloading the task's workflow status."""
    database = connect(args.db)
    try:
        transaction(database)
        task = database.execute(
            "SELECT status, archived_at, row_version FROM tasks WHERE id=?",
            (args.task_id,),
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
    """Clear ``archived_at`` while preserving the workflow status."""
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
    """Create one or more raw Operator tasks as Planner-owned drafts."""
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
                "preflight_status",
                "preflight_execution_id",
                "preflight_started_at",
                "preflight_completed_at",
                "preflight_failure",
                "lock_mode",
                "technical_acceptance",
                "preflight_evidence",
                "split_suggestions",
            }
            if set(item) & forbidden:
                raise LoopError("新任务不得预写 Planner 补充字段")
            estimate = item.get(
                "estimated_capability_level", item.get("capability_level")
            )
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
                "result": item.get("result")
                or {"summary": None, "verification": [], "error": None},
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
    """Update Operator-owned fields and invalidate previous Planner output."""
    patch = read_json(Path(args.file).resolve())
    allowed = {
        "title",
        "description",
        "priority",
        "execution_profile",
        "capability_level",
        "estimated_capability_level",
        "runtime_environment",
        "provider_id",
        "execution_policy",
    }
    unknown = set(patch) - allowed - {
        "scope",
        "scope_hint",
        "depends_on",
        "acceptance",
    }
    if unknown:
        raise LoopError("不支持的更新字段: " + ", ".join(sorted(unknown)))
    if not patch:
        raise LoopError("任务更新不能为空")
    if "capability_level" in patch and "estimated_capability_level" in patch:
        if patch["capability_level"] != patch["estimated_capability_level"]:
            raise LoopError(
                "capability_level 兼容输入与 estimated_capability_level 不一致"
            )
    database = connect(args.db)
    try:
        transaction(database)
        task = database.execute(
            "SELECT * FROM tasks WHERE id=?", (args.task_id,)
        ).fetchone()
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
                if execution_profile
                else current_payload["estimated_capability_level"],
            ),
        )
        execution_policy = patch.get(
            "execution_policy",
            ("manual" if execution_profile == "exceptional" else "automatic")
            if execution_profile
            else current_payload["execution_policy"],
        )
        if (
            estimated_capability_level is not None
            and estimated_capability_level not in CAPABILITY_LEVELS
        ):
            raise LoopError("estimated_capability_level 无效")
        if execution_policy not in {"automatic", "manual"}:
            raise LoopError("execution_policy 无效")
        if execution_profile and legacy_profile_for(
            estimated_capability_level, execution_policy
        ) != execution_profile:
            raise LoopError(
                "旧 execution_profile 与 capability_level/execution_policy 不一致"
            )
        if execution_policy == "manual" and estimated_capability_level not in {
            None,
            "L5",
        }:
            raise LoopError("人工执行策略只允许 L5")
        runtime_environment = patch.get(
            "runtime_environment", current_payload["runtime_environment"]
        )
        provider_id = patch.get("provider_id", current_payload["provider_id"])
        if patch.get("runtime_environment") == "deepseek" and "provider_id" not in patch:
            provider_id = "deepseek"
        if (
            runtime_environment != "self_hosted_agent"
            and runtime_environment != "deepseek"
            and "provider_id" not in patch
        ):
            provider_id = None
        runtime_environment, provider_id = normalize_execution_target(
            runtime_environment, provider_id
        )
        if estimated_capability_level is not None:
            resolve_execution_profile(
                runtime_environment, provider_id, estimated_capability_level
            )
        scope_hint = normalize_string_list(
            patch.get(
                "scope_hint", patch.get("scope", current_payload["scope_hint"])
            ),
            "scope_hint",
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

        # All Planner-owned output is cleared as one unit. Keeping any old
        # scope/evidence after a definition change would make claim unsafe.
        database.execute("DELETE FROM task_conflicts WHERE task_id=?", (args.task_id,))
        database.execute("DELETE FROM task_scopes WHERE task_id=?", (args.task_id,))
        replace_ordered_text(database, "task_technical_acceptance", args.task_id, [])
        replace_ordered_text(database, "task_preflight_evidence", args.task_id, [])
        if "acceptance" in patch:
            replace_ordered_text(
                database,
                "task_acceptance",
                args.task_id,
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
            (
                title.strip(),
                description,
                priority,
                estimated_capability_level,
                runtime_environment,
                provider_id,
                execution_policy,
                json_dump(scope_hint),
                stamp,
                args.task_id,
            ),
        )
        database.execute(
            "DELETE FROM task_verifications WHERE task_id=?", (args.task_id,)
        )
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, ?, 'DRAFT', 'task-manager', 'Operator 更新原始任务定义；必须重新预检。')",
            (args.task_id, stamp, previous_status),
        )
        revision = bump_revision(database, "task-manager")
        commit(database)
        output(
            {
                "outcome": "UPDATED",
                "task_id": args.task_id,
                "status": "DRAFT",
                "preflight_status": "UNINSPECTED",
                "revision": revision,
            }
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_requeue(args: argparse.Namespace) -> None:
    """Return a task either to Planner inspection or the ready Worker queue."""
    database = connect(args.db)
    try:
        transaction(database)
        row = database.execute(
            "SELECT * FROM tasks WHERE id=?", (args.task_id,)
        ).fetchone()
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
        diagnostic_reset = (
            "result_diagnostic_json=NULL, "
            if uses_result_diagnostic_schema(database)
            else ""
        )
        needs_preflight = uses_preflight_schema(database) and row["status"] in {
            "DRAFT",
            "NEEDS_REVIEW",
        }
        if needs_preflight:
            database.execute("DELETE FROM task_scopes WHERE task_id=?", (args.task_id,))
            replace_ordered_text(
                database, "task_technical_acceptance", args.task_id, []
            )
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
            (
                args.task_id,
                stamp,
                row["status"],
                new_status,
                args.reason or "人工重新排队。",
            ),
        )
        revision = bump_revision(database, "task-manager")
        commit(database)
        output(
            {
                "outcome": "REQUEUED",
                "task_id": args.task_id,
                "status": new_status,
                "preflight_status": next_preflight,
                "revision": revision,
            }
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_cancel(args: argparse.Namespace) -> None:
    """Cancel an inactive, unarchived task."""
    database = connect(args.db)
    try:
        transaction(database)
        row = database.execute(
            "SELECT * FROM tasks WHERE id=?", (args.task_id,)
        ).fetchone()
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
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, ?, 'CANCELLED', 'task-manager', ?)",
            (args.task_id, stamp, row["status"], args.reason or "任务已取消。"),
        )
        revision = bump_revision(database, "task-manager")
        commit(database)
        output(
            {"outcome": "CANCELLED", "task_id": args.task_id, "revision": revision}
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()
