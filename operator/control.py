"""Operator 与 Dashboard 共用的人工任务管理命令。

本模块负责两类操作：修改任务的原始定义，以及执行必须由人明确触发的状态流转。
它不会领取 Worker 任务，也不会直接修改业务项目。新任务和发生实质修改的旧任务
统一回到 ``DRAFT / UNINSPECTED``，由 Planner 重新生成技术范围、验收项、证据和
能力等级，避免继续使用已经失效的预检结果。

多数写命令要求调用方传入 ``expected_row_version``。该字段是浏览器操作和自动化
并发写入之间的乐观锁：页面如果基于旧版本提交，命令会拒绝覆盖较新的任务数据。
排查“页面操作无效”时，应先核对任务状态、归档时间和 row_version，再检查事务内
的状态历史与 revision 是否同时写入。
"""

from __future__ import annotations

# 中文排查：Operator 的确认、人工答复、归档、创建、更新、重排和取消都从本文件进入。
# 每个命令先校验状态与 row_version，再在单个事务中写历史；并发错误优先检查这两道门禁。
# 本角色只管理任务事实，不领取任务，也不实现任务描述中的业务代码。

import argparse
import json
from pathlib import Path

from loop_agent.control.io import output, read_json, require_expected_row_version
from loopdb import (
    ARCHIVABLE_STATUSES,
    CAPABILITY_LEVELS,
    PRIORITIES,
    LoopError,
    bump_revision,
    commit,
    configured_projects,
    connect,
    insert_task,
    json_dump,
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
)


def _normalize_attachments(task_id: str, value: object) -> list[dict[str, object]]:
    """校验新任务附件只能登记在该任务自己的 data/assets 目录。"""
    if not isinstance(value, list):
        raise LoopError("attachments 必须是数组")
    task_prefix = f"data/assets/{task_id}/"
    normalized: list[dict[str, object]] = []
    for attachment in value:
        if not isinstance(attachment, dict):
            raise LoopError("attachments 每一项必须是对象")
        path = attachment.get("path")
        if not isinstance(path, str) or not path.strip():
            raise LoopError("attachment.path 不能为空")
        relative = Path(path.strip())
        normalized_path = relative.as_posix()
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not normalized_path.startswith(task_prefix)
        ):
            raise LoopError(
                f"任务附件必须位于 {task_prefix}，当前路径: {normalized_path}"
            )
        normalized.append({**attachment, "path": normalized_path})
    return normalized


def _migrated_asset_path(value: str) -> str:
    """把旧根级附件路径转换为 data 下的新相对路径。"""
    if value.startswith("assets/"):
        return "data/" + value
    if value.startswith("local-agent-loop/assets/"):
        return "local-agent-loop/data/" + value.removeprefix("local-agent-loop/")
    return value


def command_confirm(args: argparse.Namespace) -> None:
    """把人工复核通过的 ``SUCCEEDED`` 任务标记为 ``CONFIRMED``。

    命令先校验 row_version、成功状态和未归档条件，再在同一事务内更新进度、写入
    ``task_history`` 并提升数据库 revision。该操作不归档任务，也不重新执行验证。
    """
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
    """用最终人工答复结束 Worker 留下的最后一个人工阻塞项。

    任务必须处于 ``WAITING_HUMAN``，不能存在活动 execution 或 scope 锁，并且必须
    已保存至少一条 Worker 验证记录。成功后写入人工答复、结果摘要和状态历史，状态
    变为 ``SUCCEEDED``，但仍需后续人工确认或归档。
    """
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
        if task["status"] != "WAITING_HUMAN":
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

        if not task["human_required"]:
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
        database.execute(
            """
            UPDATE tasks SET status='SUCCEEDED', updated_at=?, completed_at=?,
              progress_percent=100, progress_summary=?, progress_next_step=NULL,
              result_summary=?, result_error=NULL, result_diagnostic_json=NULL, human_required=0,
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
    """为终态任务写入独立的 ``archived_at``，不复用或改变工作流状态。

    仅 ``ARCHIVABLE_STATUSES`` 中的任务可归档。重复归档按幂等操作返回原时间；首次
    归档会增加 row_version、记录一条状态不变的历史并提升 revision。
    """
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
    """清除 ``archived_at`` 并保留任务原有工作流状态。

    未归档任务会幂等返回；已归档任务只清除归档时间并记录操作历史，不会自动重排、
    重开或回退任务。写入前必须核对当前 row_version。
    """
    database = connect(args.db)
    try:
        transaction(database)
        task = database.execute(
            "SELECT status, archived_at, row_version FROM tasks WHERE id=?", (args.task_id,)
        ).fetchone()
        if not task:
            raise LoopError("任务不存在")
        require_expected_row_version(args, task["row_version"])
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
    """从 UTF-8 JSON 文件批量创建由 Planner 接管的原始草稿任务。

    输入可以是单个任务、带 ``tasks`` 的对象或任务数组。调用方只能提供 Operator
    拥有的原始字段，不能预写 Planner 的 scope、技术验收和预检结果。每个任务都被
    强制初始化为 ``DRAFT / UNINSPECTED``，整批任务在同一事务中提交，任一失败则
    全部回滚。
    """
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
                "execution_profile",
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
                    "summary": "任务已创建；Planner 业务重建期间保持草稿状态。",
                    "completed": [],
                    "next_step": "等待 Planner 新版本形成执行契约。",
                },
                "result": item.get("result")
                or {"summary": None, "verification": [], "error": None},
                "attachments": _normalize_attachments(
                    str(item["id"]), item.get("attachments") or []
                ),
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
    """更新 Operator 可编辑字段，并整体作废旧 Planner 结果。

    命令拒绝运行中、已确认、已取消、已归档或正在预检的任务。定义一旦变化，旧的
    scope、冲突、技术验收、预检证据、执行结果和人工阻塞信息都会在同一事务中清空，
    任务回到 ``DRAFT / UNINSPECTED``。这样 Worker 不会基于旧锁范围领取新定义。
    """
    patch = read_json(Path(args.file).resolve())
    allowed = {
        "title",
        "description",
        "priority",
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
        if task["preflight_status"] in {"QUEUED", "INSPECTING"}:
            raise LoopError("任务已进入 Planner 队列，不能并发修改")
        if task["archived_at"] is not None:
            raise LoopError("已归档任务必须先取消归档")
        require_expected_row_version(args, task["row_version"])
        if "priority" in patch and patch["priority"] not in PRIORITIES:
            raise LoopError(f"任务优先级无效: {patch['priority']}")
        current_payload = task_dict(database, task)
        estimated_capability_level = patch.get(
            "estimated_capability_level",
            patch.get("capability_level", current_payload["estimated_capability_level"]),
        )
        execution_policy = patch.get(
            "execution_policy",
            current_payload["execution_policy"],
        )
        if (
            estimated_capability_level is not None
            and estimated_capability_level not in CAPABILITY_LEVELS
        ):
            raise LoopError("estimated_capability_level 无效")
        if execution_policy not in {"automatic", "manual"}:
            raise LoopError("execution_policy 无效")
        if execution_policy == "manual" and estimated_capability_level not in {
            None,
            "L5",
        }:
            raise LoopError("人工执行策略只允许 L5")
        runtime_environment = patch.get(
            "runtime_environment", current_payload["runtime_environment"]
        )
        provider_id = patch.get("provider_id", current_payload["provider_id"])
        if (
            runtime_environment != "self_hosted_agent"
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

        # Planner 拥有的派生数据必须作为一个整体清空。若定义已变但仍保留旧 scope 或
        # 预检证据，Worker 可能用错误的锁范围领取任务，造成并发修改越界。
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
            "progress_summary='任务定义已更新；Planner 业务重建期间保持草稿状态。', "
            "progress_next_step='等待 Planner 新版本形成执行契约。', "
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


def command_migrate_internal_runtime(args: argparse.Namespace) -> None:
    """把旧 Codex 路由批量迁移到当前内部 Agent，且不改变任务生命周期。"""
    runtime_environment, provider_id = normalize_execution_target(
        "self_hosted_agent", "deepseek"
    )
    database = connect(args.db)
    try:
        transaction(database)
        tasks = database.execute(
            "SELECT id, status, runtime_environment, capability_level, "
            "estimated_capability_level, row_version FROM tasks "
            "WHERE runtime_environment IN ('codex_cli', 'codex_automation') "
            "ORDER BY id"
        ).fetchall()
        running = [task["id"] for task in tasks if task["status"] == "RUNNING"]
        if running:
            raise LoopError(
                "存在 RUNNING 任务，拒绝迁移运行环境: " + ", ".join(running)
            )

        for task in tasks:
            capability_level = (
                task["capability_level"] or task["estimated_capability_level"]
            )
            if capability_level is not None:
                resolve_execution_profile(
                    runtime_environment, provider_id, capability_level
                )

        if not tasks:
            commit(database)
            output(
                {
                    "outcome": "ALREADY_MIGRATED",
                    "migrated_tasks": 0,
                    "runtime_environment": runtime_environment,
                    "provider_id": provider_id,
                }
            )
            return

        stamp = now_shanghai()
        reason = (
            args.reason
            or "旧 Codex CLI/客户端任务路由迁移到内部 self-hosted Agent。"
        )
        source_counts: dict[str, int] = {}
        for task in tasks:
            source = task["runtime_environment"]
            source_counts[source] = source_counts.get(source, 0) + 1
            database.execute(
                "UPDATE tasks SET runtime_environment=?, provider_id=?, updated_at=?, "
                "row_version=row_version+1 WHERE id=? AND row_version=?",
                (
                    runtime_environment,
                    provider_id,
                    stamp,
                    task["id"],
                    task["row_version"],
                ),
            )
            database.execute(
                "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
                "VALUES(?, ?, ?, ?, 'task-manager', ?)",
                (task["id"], stamp, task["status"], task["status"], reason),
            )

        revision = bump_revision(database, "task-manager")
        commit(database)
        output(
            {
                "outcome": "RUNTIME_TARGET_MIGRATED",
                "migrated_tasks": len(tasks),
                "source_counts": source_counts,
                "runtime_environment": runtime_environment,
                "provider_id": provider_id,
                "revision": revision,
            }
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_migrate_assets_directory(args: argparse.Namespace) -> None:
    """把旧根级附件记录迁移到 data/assets，且不改变任务生命周期。"""
    database = connect(args.db)
    try:
        transaction(database)
        tasks = database.execute(
            "SELECT id, status, scope_hint_json, lock_mode, row_version FROM tasks "
            "ORDER BY id"
        ).fetchall()
        attachments = database.execute(
            "SELECT task_id, ordinal, path FROM task_attachments ORDER BY task_id, ordinal"
        ).fetchall()
        scopes = database.execute(
            "SELECT task_id, ordinal, scope FROM task_scopes ORDER BY task_id, ordinal"
        ).fetchall()

        affected_task_ids: set[str] = set()
        migrated_attachments = 0
        migrated_scopes = 0
        for attachment in attachments:
            migrated_path = _migrated_asset_path(attachment["path"])
            if migrated_path != attachment["path"]:
                database.execute(
                    "UPDATE task_attachments SET path=? WHERE task_id=? AND ordinal=?",
                    (migrated_path, attachment["task_id"], attachment["ordinal"]),
                )
                affected_task_ids.add(attachment["task_id"])
                migrated_attachments += 1

        for scope in scopes:
            migrated_scope = _migrated_asset_path(scope["scope"])
            if migrated_scope == scope["scope"]:
                continue
            task = next(item for item in tasks if item["id"] == scope["task_id"])
            if task["lock_mode"] != "project":
                raise LoopError(
                    f"任务 {task['id']} 的附件 scope 不是 project 锁，拒绝保留旧 scope_key"
                )
            database.execute(
                "UPDATE task_scopes SET scope=? WHERE task_id=? AND ordinal=?",
                (migrated_scope, scope["task_id"], scope["ordinal"]),
            )
            affected_task_ids.add(scope["task_id"])
            migrated_scopes += 1

        migrated_scope_hints = 0
        changed_tasks: list[object] = []
        for task in tasks:
            scope_hints = json.loads(task["scope_hint_json"])
            migrated_hints = [_migrated_asset_path(value) for value in scope_hints]
            changed_hint_count = sum(
                before != after
                for before, after in zip(scope_hints, migrated_hints, strict=True)
            )
            if changed_hint_count:
                database.execute(
                    "UPDATE tasks SET scope_hint_json=? WHERE id=?",
                    (json_dump(migrated_hints), task["id"]),
                )
                affected_task_ids.add(task["id"])
                migrated_scope_hints += changed_hint_count
            if task["id"] in affected_task_ids:
                changed_tasks.append(task)

        if not affected_task_ids:
            commit(database)
            output(
                {
                    "outcome": "ALREADY_MIGRATED",
                    "migrated_attachments": 0,
                    "migrated_scope_hints": 0,
                    "migrated_scopes": 0,
                }
            )
            return

        stamp = now_shanghai()
        reason = args.reason or "任务附件目录从 assets 迁移到 data/assets。"
        for task in changed_tasks:
            database.execute(
                "UPDATE tasks SET updated_at=?, row_version=row_version+1 "
                "WHERE id=? AND row_version=?",
                (stamp, task["id"], task["row_version"]),
            )
            database.execute(
                "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
                "VALUES(?, ?, ?, ?, 'task-manager', ?)",
                (task["id"], stamp, task["status"], task["status"], reason),
            )

        revision = bump_revision(database, "task-manager")
        commit(database)
        output(
            {
                "outcome": "ASSETS_DIRECTORY_MIGRATED",
                "affected_tasks": len(affected_task_ids),
                "migrated_attachments": migrated_attachments,
                "migrated_scope_hints": migrated_scope_hints,
                "migrated_scopes": migrated_scopes,
                "revision": revision,
            }
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def command_requeue(args: argparse.Namespace) -> None:
    """按当前预检状态把任务送回 Planner 或 Worker 队列。

    ``DRAFT`` 和 ``NEEDS_REVIEW`` 会清除 Planner 派生结果并回到预检入口；其他允许
    重排的终止/等待状态只有在 preflight 已为 ``READY`` 时才能直接回到 ``PENDING``。
    两条路径都会清除旧执行结果、人工阻塞和冲突投影，但不会修改原始任务定义。
    """
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
        if row["preflight_status"] in {"QUEUED", "INSPECTING"}:
            raise LoopError("任务已进入 Planner 队列，不能重新排队")
        stamp = now_shanghai()
        database.execute("DELETE FROM task_conflicts WHERE task_id=?", (args.task_id,))
        needs_preflight = row["status"] in {
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
                "UPDATE tasks SET status='DRAFT', preflight_status='UNINSPECTED', "
                "preflight_execution_id=NULL, preflight_started_at=NULL, preflight_completed_at=NULL, "
                "preflight_failure=NULL, capability_level=NULL, lock_mode=NULL, split_suggestions_json='[]', "
                "assigned_agent=NULL, heartbeat_at=NULL, completed_at=NULL, updated_at=?, progress_percent=0, "
                "progress_summary='任务已人工送回草稿；Planner 业务正在重建。', "
                "progress_next_step='等待 Planner 新版本形成执行契约。', "
                "result_diagnostic_json=NULL, human_required=0, human_question=NULL, human_options_json='[]', "
                "human_requested_at=NULL, human_responded_at=NULL, human_response=NULL, "
                "row_version=row_version+1 WHERE id=?",
                (stamp, args.task_id),
            )
            new_status = "DRAFT"
            next_preflight = "UNINSPECTED"
        else:
            if row["preflight_status"] != "READY":
                raise LoopError("任务尚未 READY，不能直接重新排入 Worker 队列")
            database.execute(
                "UPDATE tasks SET status='PENDING', assigned_agent=NULL, heartbeat_at=NULL, completed_at=NULL, "
                "updated_at=?, progress_percent=0, progress_summary='任务已人工重新排队。', "
                "progress_next_step='等待并发 Worker 领取。', result_diagnostic_json=NULL, "
                "human_required=0, human_question=NULL, "
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
    """取消未运行、未预检占用且未归档的任务。

    命令将状态改为 ``CANCELLED``、清除下一步提示并写入历史。它不会终止活动进程，
    因此 ``RUNNING``、``QUEUED`` 或 ``INSPECTING`` 必须先由对应恢复流程释放后才能取消。
    """
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
        if row["preflight_status"] in {"QUEUED", "INSPECTING"}:
            raise LoopError("任务已进入 Planner 队列，不能取消")
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
