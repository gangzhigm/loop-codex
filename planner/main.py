"""Planner 的单实例只读任务发现与 heartbeat 入口。

Planner 业务正在重新设计。本进程当前只按初始化配置发现、选择 DRAFT 任务并维护运行状态；
不领取任务、不启动 Runner、不调用模型，也不修改任务数据库。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = REPOSITORY_ROOT / "control"
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))

from loopdb import CONFIG_PATH, DEFAULT_DB, load_initialization_config, now_shanghai
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
    """保持单实例 Planner 存活，并按配置周期只读发现 DRAFT 任务。"""
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
            query_action=lambda: select_and_report_drafts(database_path, config),
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
