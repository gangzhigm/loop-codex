"""Scheduler 的单实例双调度链与 heartbeat 入口。

预检链通过受控 loopctl 将 DRAFT/UNINSPECTED 原子排入 DRAFT/QUEUED；执行链将
PENDING/READY 自动任务原子排入 QUEUED/READY。Scheduler 不领取任务、不启动 AI Worker。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = REPOSITORY_ROOT / "control"
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))

from loopdb import CONFIG_PATH, DEFAULT_DB, load_initialization_config, now_shanghai
from common.service_runtime import ServiceRuntimeFiles, install_shutdown_signals
from scheduler.execution_dispatch import ExecutionDispatchSettings


def parser() -> argparse.ArgumentParser:
    """创建统一 Scheduler 服务的常驻命令行。"""
    value = argparse.ArgumentParser(description="Local Agent Loop Scheduler")
    commands = value.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="常驻运行 Scheduler")
    serve.add_argument("--db", default=str(DEFAULT_DB))
    serve.add_argument("--config", default=str(CONFIG_PATH))
    return value


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
            "event": "scheduler.preflight_schedule.completed",
            **payload,
        }
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        result = {
            "at": now_shanghai(),
            "event": "scheduler.preflight_schedule.failed",
            "error_type": type(error).__name__,
            "message": str(error),
        }
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def dispatch_ready_tasks(
    database_path: Path,
    config_path: Path,
    *,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, object]:
    """通过 loopctl 原子排入一个正式 execution，不启动 AI 进程。"""
    try:
        completed = command_runner(
            [
                sys.executable,
                "-B",
                str(REPOSITORY_ROOT / "control" / "loopctl.py"),
                "--db",
                str(database_path),
                "schedule-execution",
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
        result: dict[str, object] = {
            "at": now_shanghai(),
            "event": "scheduler.execution_schedule.completed",
            **payload,
        }
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        result = {
            "at": now_shanghai(),
            "event": "scheduler.execution_schedule.failed",
            "error_type": type(error).__name__,
            "message": str(error),
        }
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def run_scheduler(
    runtime: ServiceRuntimeFiles,
    pid: int,
    shutdown_event: threading.Event,
    *,
    heartbeat_interval_seconds: float,
    preflight_interval_seconds: float,
    preflight_action: Callable[[], object] | None,
    execution_interval_seconds: float | None = None,
    execution_action: Callable[[], object] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """立即运行启用的两条调度链，并让三个周期彼此独立推进。"""
    if execution_action is not None and (
        execution_interval_seconds is None or execution_interval_seconds <= 0
    ):
        raise ValueError("正式执行排队周期必须大于 0")
    next_heartbeat = monotonic()
    next_preflight = monotonic() if preflight_action is not None else float("inf")
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
        if preflight_action is not None and current >= next_preflight:
            preflight_action()
            next_preflight = monotonic() + preflight_interval_seconds
        if execution_action is not None and current >= next_execution:
            execution_action()
            next_execution = monotonic() + float(execution_interval_seconds)
        current = monotonic()
        wait_seconds = min(
            max(0.0, next_heartbeat - current),
            max(0.0, next_preflight - current),
            max(0.0, next_execution - current),
        )
        runtime.wait(shutdown_event, pid, wait_seconds)


def serve_scheduler(args: argparse.Namespace) -> None:
    """保持单实例 Scheduler 存活，并独立推进两条调度链。"""
    config_path = Path(args.config).resolve()
    database_path = Path(args.db).resolve()
    config = load_initialization_config(config_path)
    scheduler_config = config["scheduler"]
    preflight_config = scheduler_config["preflight"]
    execution_settings = ExecutionDispatchSettings.from_config(
        config, config_path=config_path
    )
    if database_path != execution_settings.database_path:
        execution_settings = replace(
            execution_settings, database_path=database_path
        )
    preflight_scheduled = preflight_config["scheduled"] is True
    execution_scheduled = execution_settings.scheduled is True
    if not preflight_scheduled and not execution_scheduled:
        raise SystemExit("Scheduler 所有调度链均已关闭")

    heartbeat_interval = float(scheduler_config["heartbeat_interval_seconds"])
    preflight_interval = float(preflight_config["interval_minutes"]) * 60
    execution_interval = float(execution_settings.interval_minutes) * 60
    runtime = ServiceRuntimeFiles.from_component_config(config, "scheduler")
    runtime.prepare()
    pid = os.getpid()
    shutdown_event = threading.Event()
    runtime.claim(pid, "Scheduler PID 文件已存在")

    try:
        install_shutdown_signals(shutdown_event)
        print(f"{now_shanghai()} scheduler started", flush=True)
        run_scheduler(
            runtime,
            pid,
            shutdown_event,
            heartbeat_interval_seconds=heartbeat_interval,
            preflight_interval_seconds=preflight_interval,
            preflight_action=(lambda: schedule_preflights(database_path, config_path))
            if preflight_scheduled
            else None,
            execution_interval_seconds=execution_interval,
            execution_action=(lambda: dispatch_ready_tasks(database_path, config_path))
            if execution_scheduled
            else None,
        )
    finally:
        shutdown_event.set()
        runtime.cleanup(pid)


def main(argv: Sequence[str] | None = None) -> None:
    """解析 serve 参数并启动 Scheduler。"""
    args = parser().parse_args(argv)
    serve_scheduler(args)


if __name__ == "__main__":
    main()
