"""Planner 的单实例任务选择、Runner 交付与 heartbeat 入口。

本进程按初始化配置发现并选择 DRAFT 任务，再把每个明确 task-id 交给独立 Planner
Runner。当前 Runner 只确认收到交付，不领取任务、不调用模型，也不修改任务数据库。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = REPOSITORY_ROOT / "control"
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))

from loopdb import CONFIG_PATH, DEFAULT_DB, load_initialization_config, now_shanghai
from common.processes import launch_detached_process
from common.service_runtime import ServiceRuntimeFiles, install_shutdown_signals
from planner.task_query import load_draft_tasks, select_draft_tasks


def parser() -> argparse.ArgumentParser:
    """创建 Planner 只读任务发现服务的常驻命令行。"""
    value = argparse.ArgumentParser(description="Local Agent Loop Planner Scheduler")
    commands = value.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="常驻运行 Planner Scheduler")
    serve.add_argument("--db", default=str(DEFAULT_DB))
    serve.add_argument("--config", default=str(CONFIG_PATH))
    return value


def query_and_report_drafts(database_path: Path) -> list[dict[str, object]]:
    """执行一次只读发现，并向 Scheduler 日志输出可核对的摘要。"""
    try:
        tasks = load_draft_tasks(database_path)
    except (OSError, sqlite3.Error, ValueError) as error:
        print(
            json.dumps(
                {
                    "at": now_shanghai(),
                    "event": "planner.draft_query.failed",
                    "error_type": type(error).__name__,
                    "message": str(error),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return []
    print(
        json.dumps(
            {
                "at": now_shanghai(),
                "event": "planner.draft_query.completed",
                "result_count": len(tasks),
                "task_ids": [task["id"] for task in tasks],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return tasks


def select_and_report_drafts(
    database_path: Path,
    config: dict[str, Any],
) -> list[dict[str, object]]:
    """按公共并发和优先级配置生成一轮只读选择计划。"""
    planner_config = config["planner"]
    priority_policy = config["priority_policy"]
    try:
        selection = select_draft_tasks(
            database_path,
            max_active_executions=int(planner_config["max_active_executions"]),
            priority_levels=priority_policy["levels"],
        )
    except (KeyError, OSError, sqlite3.Error, TypeError, ValueError) as error:
        print(
            json.dumps(
                {
                    "at": now_shanghai(),
                    "event": "planner.draft_selection.failed",
                    "error_type": type(error).__name__,
                    "message": str(error),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return []
    selected = selection.pop("selected_tasks")
    print(
        json.dumps(
            {
                "at": now_shanghai(),
                "event": "planner.draft_selection.completed",
                **selection,
                "selected_task_ids": [task["id"] for task in selected],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return selected


def start_planner_runner(
    task_id: str,
    execution_id: str,
    database_path: Path,
    config_path: Path,
    log_path: Path,
    *,
    launcher: Callable[..., int] = launch_detached_process,
) -> int:
    """把一个明确 task-id 交给阶段版 Planner Runner，并返回子进程 PID。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    return launcher(
        [
            sys.executable,
            "-B",
            str(REPOSITORY_ROOT / "runner" / "planner_runner.py"),
            "--execution-id",
            execution_id,
            "--task-id",
            task_id,
            "--db",
            str(database_path),
            "--config",
            str(config_path),
            "--log",
            str(log_path),
        ],
        REPOSITORY_ROOT,
    )


def handoff_selected_drafts(
    database_path: Path,
    config_path: Path,
    config: dict[str, Any],
    *,
    start_action: Callable[[str, str, Path, Path, Path], int] | None = None,
) -> list[dict[str, object]]:
    """选择当前可用槽位的草稿，并逐个交付给不启用 AI 的 Planner Runner。"""
    selected = select_and_report_drafts(database_path, config)
    runner_log_path = (
        REPOSITORY_ROOT
        / str(config["planner"]["scheduler"]["runner_log_path"])
    ).resolve()
    if not runner_log_path.is_relative_to(REPOSITORY_ROOT):
        raise ValueError("Planner Runner 日志路径必须位于仓库内")
    starter = start_action or (
        lambda task_id, execution_id, db, cfg, log: start_planner_runner(
            task_id, execution_id, db, cfg, log
        )
    )
    results: list[dict[str, object]] = []
    for task in selected:
        task_id = str(task["id"])
        execution_id = f"planner-{uuid.uuid4()}"
        try:
            runner_pid = starter(
                task_id,
                execution_id,
                database_path,
                config_path,
                runner_log_path,
            )
            result: dict[str, object] = {
                "at": now_shanghai(),
                "event": "planner.runner_handoff.started",
                "task_id": task_id,
                "execution_id": execution_id,
                "runner_pid": runner_pid,
                "ai_preflight_enabled": False,
            }
        except OSError as error:
            result = {
                "at": now_shanghai(),
                "event": "planner.runner_handoff.failed",
                "task_id": task_id,
                "execution_id": execution_id,
                "error_type": type(error).__name__,
                "message": str(error),
            }
        print(json.dumps(result, ensure_ascii=False), flush=True)
        results.append(result)
    return results


def run_planner_schedule(
    runtime: ServiceRuntimeFiles,
    pid: int,
    shutdown_event: threading.Event,
    *,
    heartbeat_interval_seconds: float,
    query_interval_seconds: float,
    query_action: Callable[[], object],
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """立即发现一次任务，并让查询周期与 heartbeat 周期独立推进。"""
    next_heartbeat = monotonic()
    next_query = monotonic()
    while not shutdown_event.is_set():
        if runtime.stop_requested(pid):
            break
        current = monotonic()
        if current >= next_heartbeat:
            runtime.write_heartbeat(pid)
            next_heartbeat = current + heartbeat_interval_seconds
        if current >= next_query:
            query_action()
            next_query = monotonic() + query_interval_seconds
        current = monotonic()
        wait_seconds = min(
            max(0.0, next_heartbeat - current),
            max(0.0, next_query - current),
        )
        runtime.wait(shutdown_event, pid, wait_seconds)


def serve_planner(args: argparse.Namespace) -> None:
    """保持单实例 Planner 存活，并周期选择草稿交付给阶段版 Runner。"""
    config_path = Path(args.config).resolve()
    database_path = Path(args.db).resolve()
    config = load_initialization_config(config_path)
    scheduler_config = config["planner"]["scheduler"]
    if scheduler_config["scheduled"] is not True:
        raise SystemExit("Planner Scheduler 已关闭")

    heartbeat_interval = float(scheduler_config["heartbeat_interval_seconds"])
    query_interval = float(scheduler_config["interval_minutes"]) * 60
    runtime = ServiceRuntimeFiles.from_component_config(config, "planner")
    runtime.prepare()
    pid = os.getpid()
    shutdown_event = threading.Event()
    runtime.claim(pid, "Planner Scheduler PID 文件已存在")

    try:
        install_shutdown_signals(shutdown_event)
        print(f"{now_shanghai()} planner scheduler started", flush=True)
        run_planner_schedule(
            runtime,
            pid,
            shutdown_event,
            heartbeat_interval_seconds=heartbeat_interval,
            query_interval_seconds=query_interval,
            query_action=lambda: handoff_selected_drafts(
                database_path, config_path, config
            ),
        )
    finally:
        shutdown_event.set()
        runtime.cleanup(pid)


def main(argv: Sequence[str] | None = None) -> None:
    """解析 serve 参数并启动 Planner Scheduler。"""
    args = parser().parse_args(argv)
    serve_planner(args)


if __name__ == "__main__":
    main()
