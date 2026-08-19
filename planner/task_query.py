"""Planner 的只读任务发现能力。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from loopdb import connect, list_tasks


def load_draft_tasks(database_path: Path) -> list[dict[str, Any]]:
    """读取完整任务投影，并只返回主状态为 DRAFT 的任务。"""
    database = connect(database_path)
    try:
        return list_tasks(database, statuses={"DRAFT"})
    finally:
        database.close()


def select_draft_tasks(
    database_path: Path,
    *,
    max_active_executions: int,
    priority_levels: Sequence[str],
) -> dict[str, Any]:
    """根据当前预检占用和公共优先级顺序生成一轮只读选择计划。"""
    drafts = load_draft_tasks(database_path)
    processing_count = sum(
        task["preflight_status"] == "INSPECTING" for task in drafts
    )
    available_slots = max(0, max_active_executions - processing_count)
    priority_rank = {
        priority: index for index, priority in enumerate(priority_levels)
    }
    candidates = sorted(
        (
            task
            for task in drafts
            if task["preflight_status"] == "UNINSPECTED"
        ),
        key=lambda task: (
            priority_rank.get(str(task["priority"]), len(priority_rank)),
            str(task["created_at"]),
            str(task["id"]),
        ),
    )
    selected = candidates[:available_slots]
    print(
        f"Drafts: {len(drafts)}, Processing: {processing_count}, "
        f"Candidates: {len(candidates)}, Selected: {len(selected)}"
    )
    return {
        "draft_total": len(drafts),
        "processing_count": processing_count,
        "candidate_count": len(candidates),
        "max_active_executions": max_active_executions,
        "available_slots": available_slots,
        "selected_count": len(selected),
        "selected_tasks": selected,
    }
