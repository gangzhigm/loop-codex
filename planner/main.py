"""Planner 占位服务的单实例 heartbeat 入口。

Planner 业务正在重新设计。本进程当前只维护 PID、heartbeat、停止请求和信号处理，
不领取任务、不启动 Runner，也不读写任务数据库。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path
from typing import Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = REPOSITORY_ROOT / "control"
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))

from loopdb import CONFIG_PATH, DEFAULT_DB, load_initialization_config, now_shanghai
from common.service_runtime import ServiceRuntimeFiles, install_shutdown_signals


def parser() -> argparse.ArgumentParser:
    """创建 Planner heartbeat 占位服务的常驻命令行。"""
    value = argparse.ArgumentParser(description="Local Agent Loop Planner heartbeat service")
    commands = value.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="常驻运行 Planner heartbeat 服务")
    serve.add_argument("--db", default=str(DEFAULT_DB))
    serve.add_argument("--config", default=str(CONFIG_PATH))
    return value


def serve_planner(args: argparse.Namespace) -> None:
    """保持单实例占位服务存活，并持续向 Supervisor 提供 heartbeat。"""
    config_path = Path(args.config).resolve()
    config = load_initialization_config(config_path)
    scheduler_config = config["planner"]["scheduler"]
    if scheduler_config["scheduled"] is not True:
        raise SystemExit("Planner heartbeat 服务已关闭")

    heartbeat_interval = float(scheduler_config["heartbeat_interval_seconds"])
    runtime = ServiceRuntimeFiles.from_component_config(config, "planner")
    runtime.prepare()
    pid = os.getpid()
    shutdown_event = threading.Event()
    runtime.claim(pid, "Planner heartbeat 服务 PID 文件已存在")

    try:
        install_shutdown_signals(shutdown_event)
        print(f"{now_shanghai()} planner heartbeat service started", flush=True)
        while not shutdown_event.is_set():
            runtime.write_heartbeat(pid)
            runtime.wait(shutdown_event, pid, heartbeat_interval)
    finally:
        runtime.cleanup(pid)


def main(argv: Sequence[str] | None = None) -> None:
    """解析 serve 参数并启动 Planner heartbeat 占位服务。"""
    args = parser().parse_args(argv)
    serve_planner(args)


if __name__ == "__main__":
    main()
