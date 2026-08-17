"""Self-hosted Agent Dispatcher 的单实例常驻调度入口。

本进程维护自己的 PID 与 heartbeat，并按配置周期执行一次现有 Dispatcher 调度。
Supervisor 只管理本进程，不读取任务数据库，也不直接启动 Runner。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = REPOSITORY_ROOT / "control"
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))

from loopdb import CONFIG_PATH, DEFAULT_DB, load_initialization_config, now_shanghai
from common.scheduler import SchedulerRuntimeFiles, install_shutdown_signals
from dispatcher.agent_dispatcher import AgentDispatcher, DispatcherSettings


def parser() -> argparse.ArgumentParser:
    """创建 Dispatcher Scheduler 常驻命令行。"""
    value = argparse.ArgumentParser(description="Local Agent Loop Dispatcher Scheduler")
    commands = value.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="常驻运行 Dispatcher Scheduler")
    serve.add_argument("--db", default=str(DEFAULT_DB))
    serve.add_argument("--config", default=str(CONFIG_PATH))
    return value


def serve_dispatcher(args: argparse.Namespace) -> None:
    """保持单实例 Dispatcher 存活，并按配置周期执行现有单轮调度。"""
    config_path = Path(args.config).resolve()
    database_path = Path(args.db).resolve()
    config = load_initialization_config(config_path)
    settings = DispatcherSettings.from_config(config, config_path=config_path)
    if settings.scheduled is not True:
        raise SystemExit("Dispatcher 自动调度已关闭")
    if database_path != settings.database_path:
        settings = DispatcherSettings(**{**settings.__dict__, "database_path": database_path})

    heartbeat_interval = float(settings.heartbeat_interval_seconds)
    dispatch_interval = float(settings.interval_minutes) * 60
    runtime = SchedulerRuntimeFiles.from_config(config, "dispatcher")
    runtime.prepare()
    pid = os.getpid()
    shutdown_event = threading.Event()
    runtime.claim(pid, "Dispatcher Scheduler PID 文件已存在")

    try:
        install_shutdown_signals(shutdown_event)
        print(f"{now_shanghai()} dispatcher scheduler started", flush=True)
        next_heartbeat = time.monotonic()
        next_dispatch = time.monotonic()
        while not shutdown_event.is_set():
            # 停止 Scheduler 只会阻止后续分发，已经启动的 Runner 独立完成自己的任务。
            if runtime.stop_requested():
                break
            current = time.monotonic()
            if current >= next_heartbeat:
                runtime.write_heartbeat(pid)
                next_heartbeat = current + heartbeat_interval
            if current >= next_dispatch:
                result = AgentDispatcher(settings, config).run()
                print(json.dumps(result, ensure_ascii=False), flush=True)
                next_dispatch = time.monotonic() + dispatch_interval
            current = time.monotonic()
            wait_seconds = min(
                1.0,
                max(0.0, next_heartbeat - current),
                max(0.0, next_dispatch - current),
            )
            shutdown_event.wait(wait_seconds)
    finally:
        shutdown_event.set()
        runtime.cleanup(pid)


def main(argv: Sequence[str] | None = None) -> None:
    """解析 serve 参数并启动 Dispatcher Scheduler。"""
    args = parser().parse_args(argv)
    serve_dispatcher(args)


if __name__ == "__main__":
    main()
