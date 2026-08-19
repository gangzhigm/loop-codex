"""Planner 的单实例双调度链与 heartbeat 入口。

预检链通过受控 loopctl 将 DRAFT/UNINSPECTED 原子排入 DRAFT/QUEUED；执行链从
PENDING/READY 中选择自动任务并启动独立 Runner。Planner 本身不领取任务、不调用模型。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import replace
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
from planner.execution_dispatch import (
    ExecutionDispatchError,
    ExecutionDispatcher,
    ExecutionDispatchSettings,
)
from planner.task_query import load_draft_tasks, select_draft_tasks


def parser() -> argparse.ArgumentParser:
    """创建 Planner 预检排队服务的常驻命令行。"""
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


def schedule_preflights(
    database_path: Path,
    config_path: Path,
    *,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, object]:
    """通过 loopctl 执行一轮原子排队，并记录可核对的结构化结果。"""
    try:
        completed = command_runner(
            [
                sys.executable,
                "-B",
                str(REPOSITORY_ROOT / "control" / "loopctl.py"),
                "--db",
                str(database_path),
                "schedule-preflight",
                "--config",
                str(config_path),
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        payload = json.loads(completed.stdout)
        if completed.returncode != 0:
            raise RuntimeError(str(payload.get("message") or completed.stderr.strip()))
        result = {
            "at": now_shanghai(),
            "event": "planner.preflight_schedule.completed",
            **payload,
        }
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        result = {
            "at": now_shanghai(),
            "event": "planner.preflight_schedule.failed",
            "error_type": type(error).__name__,
            "message": str(error),
        }
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def dispatch_ready_tasks(
    settings: ExecutionDispatchSettings,
    config: dict[str, Any],
) -> dict[str, object]:
    """执行一轮正式任务分发，并把结果写入 Planner Scheduler 日志。"""
    try:
        payload = ExecutionDispatcher(settings, config).run()
        result: dict[str, object] = {
            "at": now_shanghai(),
            "event": "planner.execution_dispatch.completed",
            **payload,
        }
    except (ExecutionDispatchError, OSError, sqlite3.Error, ValueError) as error:
        result = {
            "at": now_shanghai(),
            "event": "planner.execution_dispatch.failed",
            "error_type": type(error).__name__,
            "message": str(error),
        }
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def run_planner_schedule(
    runtime: ServiceRuntimeFiles,
    pid: int,
    shutdown_event: threading.Event,
    *,
    heartbeat_interval_seconds: float,
    query_interval_seconds: float,
    query_action: Callable[[], object] | None,
    execution_interval_seconds: float | None = None,
    execution_action: Callable[[], object] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """立即运行启用的两条调度链，并让三个周期彼此独立推进。"""
    if execution_action is not None and (
        execution_interval_seconds is None or execution_interval_seconds <= 0
    ):
        raise ValueError("正式执行分发周期必须大于 0")
    next_heartbeat = monotonic()
    next_query = monotonic() if query_action is not None else float("inf")
    next_execution = (
        monotonic() if execution_action is not None else float("inf")
    )
    while not shutdown_event.is_set():
        if runtime.stop_requested(pid):
            break
        current = monotonic()
        if current >= next_heartbeat:
            runtime.write_heartbeat(pid)
            next_heartbeat = current + heartbeat_interval_seconds
        if query_action is not None and current >= next_query:
            query_action()
            next_query = monotonic() + query_interval_seconds
        if execution_action is not None and current >= next_execution:
            execution_action()
            next_execution = monotonic() + float(execution_interval_seconds)
        current = monotonic()
        wait_seconds = min(
            max(0.0, next_heartbeat - current),
            max(0.0, next_query - current),
            max(0.0, next_execution - current),
        )
        runtime.wait(shutdown_event, pid, wait_seconds)


def serve_planner(args: argparse.Namespace) -> None:
    """保持单实例 Planner 存活，并周期调用受控预检排队命令。"""
    config_path = Path(args.config).resolve()
    database_path = Path(args.db).resolve()
    config = load_initialization_config(config_path)
    scheduler_config = config["planner"]["scheduler"]
    execution_settings = ExecutionDispatchSettings.from_config(
        config, config_path=config_path
    )
    if database_path != execution_settings.database_path:
        execution_settings = replace(
            execution_settings, database_path=database_path
        )
    preflight_scheduled = scheduler_config["scheduled"] is True
    execution_scheduled = execution_settings.scheduled is True
    if not preflight_scheduled and not execution_scheduled:
        raise SystemExit("Planner 所有调度链均已关闭")

    heartbeat_interval = float(scheduler_config["heartbeat_interval_seconds"])
    query_interval = float(scheduler_config["interval_minutes"]) * 60
    execution_interval = float(execution_settings.interval_minutes) * 60
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
            query_action=(lambda: schedule_preflights(database_path, config_path))
            if preflight_scheduled
            else None,
            execution_interval_seconds=execution_interval,
            execution_action=(lambda: dispatch_ready_tasks(execution_settings, config))
            if execution_scheduled
            else None,
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
