"""Planner 选中任务后的阶段性交付接收入口。

本阶段只确认 Scheduler 已把明确的 task-id 交给独立 Runner 进程。它只读核对任务，
不领取预检 execution、不调用 Provider、不执行 AI 预检，也不写回任务状态。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Sequence

sys.dont_write_bytecode = True

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = REPOSITORY_ROOT / "control"
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))

from loopdb import (
    CONFIG_PATH,
    DEFAULT_DB,
    connect,
    load_initialization_config,
    now_shanghai,
    task_dict,
)


class PlannerRunnerError(RuntimeError):
    """交付参数或当前任务状态不满足阶段入口。"""


def receive_planner_task(
    database_path: Path,
    config: dict[str, Any],
    *,
    execution_id: str,
    task_id: str,
) -> dict[str, Any]:
    """只读确认指定任务仍可交付，并返回不包含业务正文的接收回执。"""
    if not execution_id or len(execution_id) > 128:
        raise PlannerRunnerError("Planner execution-id 无效")
    if not task_id:
        raise PlannerRunnerError("Planner task-id 不能为空")
    planner = config.get("planner")
    if not isinstance(planner, dict):
        raise PlannerRunnerError("Planner 配置缺失")

    database = connect(database_path)
    try:
        row = database.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise PlannerRunnerError("Planner 交付的任务不存在")
        task = task_dict(database, row)
    finally:
        database.close()

    if task["status"] != "DRAFT" or task["preflight_status"] != "UNINSPECTED":
        raise PlannerRunnerError("Planner 交付的任务已不处于 DRAFT/UNINSPECTED")
    if task["runtime_environment"] != planner.get("default_runtime_environment"):
        raise PlannerRunnerError("Planner 交付任务的运行环境与配置不匹配")
    if task["provider_id"] != planner.get("provider_id"):
        raise PlannerRunnerError("Planner 交付任务的 Provider 与配置不匹配")

    return {
        "at": now_shanghai(),
        "outcome": "PLANNER_TASK_RECEIVED",
        "execution_kind": "PLANNER",
        "execution_id": execution_id,
        "task_id": task_id,
        "status": task["status"],
        "preflight_status": task["preflight_status"],
        "next_action": "AI_PREFLIGHT_NOT_ENABLED",
    }


def parser() -> argparse.ArgumentParser:
    """创建阶段性交付接收命令行。"""
    value = argparse.ArgumentParser(description="Planner Runner handoff receiver")
    value.add_argument("--execution-id", required=True)
    value.add_argument("--task-id", required=True)
    value.add_argument("--config", default=str(CONFIG_PATH))
    value.add_argument("--db", default=str(DEFAULT_DB))
    value.add_argument("--log")
    return value


def emit_receipt(result: dict[str, Any], log_path: Path | None) -> None:
    """输出回执；后台交付时由 Runner 自己追加日志，避免共享文件偏移。"""
    line = json.dumps(result, ensure_ascii=False)
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line + "\n")
            stream.flush()
    print(line, flush=True)


def main(argv: Sequence[str] | None = None) -> None:
    """核对交付并输出一条 UTF-8 JSON 回执。"""
    args = parser().parse_args(argv)
    config_path = Path(args.config).resolve()
    config = load_initialization_config(config_path)
    log_path = Path(args.log).resolve() if args.log else None
    if log_path is not None:
        expected_log = (
            REPOSITORY_ROOT
            / str(config["planner"]["scheduler"]["runner_log_path"])
        ).resolve()
        if log_path != expected_log:
            raise PlannerRunnerError("Planner Runner 日志路径与配置不匹配")
    try:
        result = receive_planner_task(
            Path(args.db).resolve(),
            config,
            execution_id=args.execution_id,
            task_id=args.task_id,
        )
    except (OSError, sqlite3.Error, PlannerRunnerError, ValueError) as error:
        emit_receipt(
            {
                "at": now_shanghai(),
                "outcome": "PLANNER_TASK_REJECTED",
                "execution_id": args.execution_id,
                "task_id": args.task_id,
                "error_type": type(error).__name__,
                "message": str(error),
            },
            log_path,
        )
        raise SystemExit(1) from None
    emit_receipt(result, log_path)


if __name__ == "__main__":
    main()
