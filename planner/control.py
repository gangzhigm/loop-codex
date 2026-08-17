"""Planner 预检的预留、心跳与结果状态机。

Planner execution 使用独立并发容量，不占用 Worker 名额，也不获取业务 scope 锁。
预检期间任务始终保持 ``DRAFT``，通过 ``preflight_execution_id`` 与 row_version 防止
迟到或并发结果覆盖新定义。``READY`` 会在同一事务内写入最终 scope、锁模式、技术
验收、证据和能力等级，再把任务送入 ``PENDING``；需要决策或预检失败则转交 Operator，
只保存拆分建议，不由 Planner 自行创建子任务。
"""

from __future__ import annotations

# 中文排查：Planner 预检状态机在此实现，核心路径是 claim、heartbeat、ready/review/fail。
# 预检异常先核对 execution fencing 和 UTF-8 stdin，再检查 scope、能力等级及人工升级门禁。
# Planner 不获取业务写锁、不实现任务；READY 写回必须与 DRAFT -> PENDING 同事务完成。

import argparse
import sqlite3
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
    json_dump,
    load_initialization_config,
    normalize_scope,
    normalize_split_suggestions,
    normalize_string_list,
    now_shanghai,
    replace_ordered_text,
    resolve_execution_profile,
    rollback,
    task_dict,
    transaction,
)


PLANNER_ESCALATION_MARKERS = {
    "L5": "APPROVED_PLANNER_ESCALATION: L5",
    "manual": "APPROVED_PLANNER_ESCALATION: manual",
}


def recover_timed_out_preflights(database: sqlite3.Connection) -> list[str]:
    """回收心跳停滞、租约过期或超过总时限的 Planner execution。

    满足任一超时信号的 execution 会标记 ``TIMED_OUT``；仅当任务仍由该 execution
    fenced 且保持 DRAFT/INSPECTING 时，才把任务恢复为 UNINSPECTED 并写历史。调用方
    必须已开启事务，本函数不自行提交。
    """
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
    """生成 Planner 可见的最小任务投影，避免暴露 Worker 执行结果和无关字段。"""
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
    """检查 Operator 是否在描述或验收项中逐行写入了精确升级批准标记。

    L5 与 manual 都属于 Planner 不能自行决定的升级；使用整行不区分大小写匹配，
    避免普通自然语言提到等级时被误识别为授权。
    """
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
    """原子预留一个最高优先级的未检查草稿任务。

    入口强制使用配置指定的 Runtime 和只读 sandbox。事务内先回收超时预检、检查
    Planner 独立容量，再按优先级和创建时间选择任务；execution 插入与任务 fencing
    更新必须同时成功。输出只包含 Operator 原始定义和只读客户端边界。
    """
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
            "AND runtime_environment=? AND provider_id=? "
            "ORDER BY CASE priority WHEN 'blocker' THEN 0 WHEN 'critical' THEN 1 WHEN 'high' THEN 2 "
            "WHEN 'medium' THEN 3 ELSE 4 END, created_at, id LIMIT 1",
            (
                config["planner"]["default_runtime_environment"],
                config["planner"]["provider_id"],
            ),
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
    """续期活动 Planner execution，并同步增加任务 row_version。

    心跳前会先执行超时回收，因此已经过期的 execution 无法被迟到心跳复活。只有
    execution_id、task_id 与任务当前 fencing 全部一致时才延长租约。
    """
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
    """校验 Planner 结束写回的 execution/task fencing，并处理幂等重放。

    若 execution 已以相同期望 outcome 完成，则返回 ``(None, execution)`` 供调用方
    输出 ALREADY_FINISHED；不同 outcome 的迟到结果会被拒绝。活动结果还必须匹配
    DRAFT/INSPECTING、preflight_execution_id 和 expected row_version。
    """
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
    """验证 READY 报告并原子发布最终 Worker 执行契约。

    报告必须精确包含摘要、能力等级、scope、锁模式、技术验收和证据，所有关键列表
    均不能为空。scope 先按锁模式规范化并检查重复；L5 或 manual 必须已有 Operator
    明确批准。提交时替换 Planner 派生表、结束预检 execution，并把任务从 DRAFT
    转为 PENDING。
    """
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
    """保存需要 Operator 决策的预检结论和可选拆分建议。

    该路径清除可执行 scope 与技术验收，保留预检证据，将任务转为 NEEDS_REVIEW，
    并把问题、选项和拆分建议作为人工输入材料。Planner 只建议，不在这里创建任务。
    """
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
    """记录无法形成有效预检结论的失败，并把修正权交回 Operator。

    FAILED 报告必须提供摘要、错误和至少一条证据。任务转为 NEEDS_REVIEW，清空可执行
    范围和能力等级，预检 execution 记录失败原因；后续只能修正定义后重新预检或取消。
    """
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
