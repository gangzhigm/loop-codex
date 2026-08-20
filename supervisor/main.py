"""仅支持 Windows 的 Supervisor 常驻监控进程。

``serve`` 根据初始化配置监控并恢复独立运行的 Dashboard、Scheduler 与 Runner。
组件管理、运行文件和 Windows 进程方法集中在根目录
``common``，本文件只保留命令行入口和长期监控循环。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path
from typing import Sequence


# 直接运行本文件时，先建立项目根目录和控制面模块搜索路径。
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = REPOSITORY_ROOT / "control"
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from loopdb import CONFIG_PATH, DEFAULT_DB, load_initialization_config, now_shanghai
from common.components import (
    component_snapshot,
    component_specs,
    record_monitor_state,
)
from common.service_control import service_control_state
from common.service_runtime import ServiceRuntimeFiles, install_shutdown_signals


def parser() -> argparse.ArgumentParser:
    """创建只用于启动常驻 Supervisor 的命令行。"""
    value = argparse.ArgumentParser(description="Local Agent Loop Supervisor entry point")
    commands = value.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="常驻运行 Supervisor 主进程")
    serve.add_argument("--db", default=str(DEFAULT_DB))
    serve.add_argument("--config", default=str(CONFIG_PATH))
    serve.add_argument(
        "--monitor-interval-seconds",
        type=float,
        help="临时覆盖初始化配置中的组件检查周期（至少 1 秒）",
    )
    return value


def serve_supervisor(args: argparse.Namespace) -> None:
    """持续确保三个独立服务进程符合配置状态。"""
    config_path = Path(args.config).resolve()
    database_path = Path(args.db).resolve()
    config = load_initialization_config(config_path)
    initial_interval = float(config["supervisor"]["monitor_interval_seconds"])
    if args.monitor_interval_seconds is not None and args.monitor_interval_seconds < 1:
        raise SystemExit("--monitor-interval-seconds 必须至少为 1")
    monitor_interval = args.monitor_interval_seconds or initial_interval
    shutdown_event = threading.Event()
    pid = os.getpid()
    runtime = ServiceRuntimeFiles.supervisor()
    runtime.prepare()
    runtime.claim(
        pid,
        "Supervisor PID 文件已存在；请通过 health 任务检查或恢复现有主进程",
    )

    try:
        install_shutdown_signals(shutdown_event)
        print(f"{now_shanghai()} supervisor monitoring started", flush=True)

        while not shutdown_event.is_set():
            if runtime.stop_requested(pid):
                break
            # 每轮重读配置，使服务开关无需重启 Supervisor 即可生效。
            config = load_initialization_config(config_path)
            desired_states = service_control_state()
            if not desired_states["supervisor"]:
                break
            specs = component_specs(config, desired_states)
            start_timeout = float(config["supervisor"]["component_start_timeout_seconds"])
            stop_timeout = float(config["supervisor"]["component_stop_timeout_seconds"])
            if args.monitor_interval_seconds is None:
                monitor_interval = float(config["supervisor"]["monitor_interval_seconds"])
            monitors = component_snapshot(
                specs,
                database_path,
                config_path,
                start_timeout_seconds=start_timeout,
                stop_timeout_seconds=stop_timeout,
            )
            record_monitor_state(monitors, supervisor_pid=pid)
            runtime.write_heartbeat(pid)
            runtime.wait(shutdown_event, pid, monitor_interval)
    finally:
        runtime.cleanup(pid)


def main(argv: Sequence[str] | None = None) -> None:
    """解析 serve 参数并启动长期运行的 Supervisor。"""
    args = parser().parse_args(argv)
    serve_supervisor(args)


if __name__ == "__main__":
    main()
