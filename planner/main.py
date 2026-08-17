"""Planner 自动调度器的单实例常驻入口。

本进程维护 Planner Scheduler 的运行生命周期和 heartbeat，并按配置周期启动一个独立
Planner Runner。Runner 自行领取一个草稿、完成 AI 预检和写回；Scheduler 启动成功后
不再持有 Runner。Supervisor 只管理 Scheduler，并只读观察动态 Runner 状态。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = REPOSITORY_ROOT / "control"
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))

from loopdb import CONFIG_PATH, DEFAULT_DB, load_initialization_config, now_shanghai
from common.processes import launch_detached_process
from common.scheduler import SchedulerRuntimeFiles, install_shutdown_signals


def parser() -> argparse.ArgumentParser:
    """创建 Planner Scheduler 常驻命令行。"""
    value = argparse.ArgumentParser(description="Local Agent Loop Planner Scheduler")
    commands = value.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="常驻运行 Planner Scheduler")
    serve.add_argument("--db", default=str(DEFAULT_DB))
    serve.add_argument("--config", default=str(CONFIG_PATH))
    return value


def start_planner_runner(
    database_path: Path,
    config_path: Path,
    log_path: Path,
) -> int:
    """启动独立 Planner Runner，返回 PID 后不再持有或管理其进程。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_stream = log_path.open("a", encoding="utf-8", newline="\n")
    execution_id = f"planner-{uuid.uuid4()}"
    try:
        return launch_detached_process(
            [
                sys.executable,
                "-B",
                str(REPOSITORY_ROOT / "runner" / "planner_runner.py"),
                "--execution-id",
                execution_id,
                "--db",
                str(database_path),
                "--config",
                str(config_path),
            ],
            REPOSITORY_ROOT,
            stdout=log_stream,
            stderr=log_stream,
        )
    finally:
        # 子进程已经取得日志句柄，Scheduler 不承担其后续日志生命周期。
        log_stream.close()


def serve_planner(args: argparse.Namespace) -> None:
    """保持单实例 Scheduler 存活，并持续向 Supervisor 提供 heartbeat。"""
    config_path = Path(args.config).resolve()
    database_path = Path(args.db).resolve()
    config = load_initialization_config(config_path)
    scheduler_config = config["planner"]["scheduler"]
    if scheduler_config["scheduled"] is not True:
        raise SystemExit("Planner 自动调度已关闭")

    heartbeat_interval = float(scheduler_config["heartbeat_interval_seconds"])
    dispatch_interval = float(scheduler_config["interval_minutes"]) * 60
    runner_log_path = (
        REPOSITORY_ROOT / str(scheduler_config["runner_log_path"])
    ).resolve()
    runtime = SchedulerRuntimeFiles.from_config(config, "planner")
    runtime.prepare()
    pid = os.getpid()
    shutdown_event = threading.Event()
    runtime.claim(pid, "Planner Scheduler PID 文件已存在")

    try:
        install_shutdown_signals(shutdown_event)
        print(f"{now_shanghai()} planner scheduler started", flush=True)
        next_heartbeat = time.monotonic()
        next_dispatch = time.monotonic()
        while not shutdown_event.is_set():
            # Supervisor 通过项目内请求文件通知常驻进程正常退出。
            if runtime.stop_requested():
                break
            current = time.monotonic()
            if current >= next_heartbeat:
                runtime.write_heartbeat(pid)
                next_heartbeat = current + heartbeat_interval
            if current >= next_dispatch:
                try:
                    runner_pid = start_planner_runner(
                        database_path, config_path, runner_log_path
                    )
                    print(
                        json.dumps(
                            {"outcome": "PLANNER_RUNNER_STARTED", "runner_pid": runner_pid},
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                except OSError as error:
                    print(
                        json.dumps(
                            {
                                "outcome": "PLANNER_RUNNER_START_FAILED",
                                "error": type(error).__name__,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                next_dispatch = time.monotonic() + dispatch_interval
            current = time.monotonic()
            shutdown_event.wait(
                min(
                    1.0,
                    max(0.0, next_heartbeat - current),
                    max(0.0, next_dispatch - current),
                )
            )
    finally:
        runtime.cleanup(pid)


def main(argv: Sequence[str] | None = None) -> None:
    """解析 serve 参数并启动 Planner Scheduler。"""
    args = parser().parse_args(argv)
    serve_planner(args)


if __name__ == "__main__":
    main()
