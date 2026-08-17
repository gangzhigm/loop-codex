"""仅支持 Windows 的 Supervisor 常驻宿主。

``serve`` 托管 Dashboard，并根据初始化配置管理 Planner Scheduler 与 Dispatcher
Scheduler。组件管理、运行文件和 Windows 进程方法集中在根目录 ``common``，
本文件只保留命令行入口、Dashboard 线程和长期监控循环。
"""

from __future__ import annotations

import argparse
import os
import signal
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

from client import dashboard_server
from loopdb import CONFIG_PATH, DEFAULT_DB, load_initialization_config, now_shanghai
from common.components import (
    ComponentSpec,
    append_monitor_fallback,
    component_health,
    component_snapshot,
    component_specs,
    ensure_component,
    heartbeat_belongs_to,
    is_component_process,
    read_component_heartbeat,
    read_monitor_state,
    record_monitor_state,
    remove_runtime_files,
    request_component_stop,
    start_component,
    wait_component_started,
    write_monitor_state,
    write_supervisor_heartbeat,
)
from common.files import read_pid
from common.paths import (
    FALLBACK_LOG,
    HEALTH_STATE,
    HEARTBEAT_PATH,
    PID_PATH,
    RUNTIME_DIR,
)
from common.windows import process_alive, windows_powershell


def parser() -> argparse.ArgumentParser:
    """创建只用于启动常驻 Supervisor 的命令行。"""
    value = argparse.ArgumentParser(description="Local Agent Loop Supervisor entry point")
    commands = value.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="常驻运行 Supervisor 主进程")
    serve.add_argument("--db", default=str(DEFAULT_DB))
    serve.add_argument("--config", default=str(CONFIG_PATH))
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument(
        "--monitor-interval-seconds",
        type=float,
        help="临时覆盖初始化配置中的组件检查周期（至少 1 秒）",
    )
    return value


def command_arguments(args: argparse.Namespace) -> list[str]:
    """把 Supervisor 入口参数转换为 Dashboard 使用的参数列表。"""
    values = ["--db", str(args.db), "--config", str(args.config)]
    if args.host is not None:
        values.extend(["--host", str(args.host)])
    if args.port is not None:
        values.extend(["--port", str(args.port)])
    return values


class DashboardThread:
    """在 Supervisor 进程内启动、停止和重启 Dashboard HTTP 服务。"""

    def __init__(self, arguments: list[str]) -> None:
        # 参数在异常重启时保持不变，配置变更通过重启 Supervisor 生效。
        self.arguments = arguments
        self.shutdown_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        """没有活动线程时启动 Dashboard；已有线程时保持幂等。"""
        if self.thread is not None and self.thread.is_alive():
            return
        self.shutdown_event = threading.Event()
        self.last_error = None

        def target() -> None:
            """把 Dashboard 阻塞循环限制在独立线程中。"""
            try:
                dashboard_server.main(
                    self.arguments,
                    install_signal_handlers=False,
                    shutdown_event=self.shutdown_event,
                )
            except Exception as error:
                # 长期状态只保存异常类型，避免日志上下文进入健康快照。
                self.last_error = type(error).__name__

        self.thread = threading.Thread(target=target, name="dashboard-server", daemon=True)
        self.thread.start()

    def is_running(self) -> bool:
        """判断 Dashboard 托管线程是否仍在运行。"""
        return self.thread is not None and self.thread.is_alive()

    def stop(self) -> None:
        """通知 Dashboard 关闭，并给线程最多十秒完成清理。"""
        self.shutdown_event.set()
        if self.thread is not None:
            self.thread.join(timeout=10)


def serve_supervisor(args: argparse.Namespace) -> None:
    """托管 Dashboard，并持续确保两个 Scheduler 符合配置状态。"""
    config_path = Path(args.config).resolve()
    database_path = Path(args.db).resolve()
    config = load_initialization_config(config_path)
    initial_interval = float(config["supervisor"]["monitor_interval_seconds"])
    if args.monitor_interval_seconds is not None and args.monitor_interval_seconds < 1:
        raise SystemExit("--monitor-interval-seconds 必须至少为 1")
    monitor_interval = args.monitor_interval_seconds or initial_interval
    shutdown_event = threading.Event()
    dashboard = DashboardThread(command_arguments(args))
    pid = os.getpid()
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    try:
        descriptor = os.open(PID_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise SystemExit(
            "Supervisor PID 文件已存在；请通过 health 任务检查或恢复现有主进程"
        ) from None
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(str(pid))

    def stop(signum: int, frame: object) -> None:
        """把系统终止信号转换为主循环可观察的停止事件。"""
        del signum, frame
        shutdown_event.set()

    try:
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        dashboard.start()
        print(f"{now_shanghai()} supervisor monitoring started", flush=True)

        while not shutdown_event.is_set():
            # 每轮重读配置，使 Planner 和 Dispatcher 开关无需重启 Supervisor 即可生效。
            config = load_initialization_config(config_path)
            specs = component_specs(config)
            start_timeout = float(config["supervisor"]["component_start_timeout_seconds"])
            stop_timeout = float(config["supervisor"]["component_stop_timeout_seconds"])
            if args.monitor_interval_seconds is None:
                monitor_interval = float(config["supervisor"]["monitor_interval_seconds"])
            if not dashboard.is_running():
                dashboard.start()
            monitors = component_snapshot(
                specs,
                database_path,
                config_path,
                dashboard_running=dashboard.is_running(),
                dashboard_error=dashboard.last_error,
                start_timeout_seconds=start_timeout,
                stop_timeout_seconds=stop_timeout,
            )
            record_monitor_state(monitors)
            write_supervisor_heartbeat(pid)
            shutdown_event.wait(monitor_interval)
    finally:
        dashboard.stop()
        if PID_PATH.exists() and PID_PATH.read_text(encoding="utf-8").strip() == str(pid):
            PID_PATH.unlink()
        if heartbeat_belongs_to(pid):
            HEARTBEAT_PATH.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> None:
    """解析 serve 参数并启动长期运行的 Supervisor。"""
    args = parser().parse_args(argv)
    serve_supervisor(args)


if __name__ == "__main__":
    main()
