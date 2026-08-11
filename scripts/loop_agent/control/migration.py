"""Explicit legacy-import and SQLite schema-migration commands.

Legacy JSON import is deliberately separate from empty-environment Bootstrap
and from in-place SQLite upgrades. It always snapshots the input files first,
refuses a populated target unless force was explicitly supplied, and validates
the final database before committing.
"""

from __future__ import annotations

# 中文排查：本模块区分旧 JSON 导入与 SQLite Schema 升级，两者都不是新环境初始化。
# 迁移失败先检查输入快照、目标库是否为空、项目清单解析和最终 validate_database 结果。
# 写入由外层事务统一提交；中途异常必须回滚，不能留下部分导入的任务。

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from loop_agent.control.io import output, read_json
from loopdb import (
    LoopError,
    commit,
    connect,
    initialize_schema,
    insert_task,
    load_initialization_config,
    migrate_schema,
    now_shanghai,
    parse_project_registry,
    rollback,
    transaction,
    validate_database,
)


LEGACY_STATUS_MAP = {
    "CLAIMED": "WAITING_HUMAN",
    "BLOCKED": "WAITING_HUMAN",
    "STALLED": "WAITING_HUMAN",
}


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



