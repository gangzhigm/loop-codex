"""Supervisor 的常驻宿主与一次性健康检查入口。

``serve`` 是长期运行的 Supervisor 主进程：它托管本机 Dashboard，并周期性检查
Dashboard、Planner 与 Codex CLI Dispatcher 的可核实状态。它只读取任务投影和本机
进程/计划任务信息，不领取任务、不管理 Codex 自动化，也不直接写 SQLite。

``health`` 由 Windows 计划任务周期调用，用于探测和恢复整个 ``serve`` 进程。
"""

from __future__ import annotations

# 中文排查：计划任务调用 health；由 health 恢复的常驻主进程调用 serve。
# 本文件只组织 Supervisor 命令边界，任务状态写入仍只能经过 scripts/loopctl.py。

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from loopdb import CONFIG_PATH, DEFAULT_DB, connect, load_initialization_config, now_shanghai
if __package__:
    from . import health_run
    from client import dashboard_server
else:
    # 计划任务以 ``python supervisor/main.py`` 直接执行时，没有包上下文。
    import health_run
    from client import dashboard_server


def parser() -> argparse.ArgumentParser:
    """创建 Supervisor 命令行，明确区分短暂检查和常驻服务。"""
    value = argparse.ArgumentParser(description="Local Agent Loop Supervisor entry point")
    commands = value.add_subparsers(dest="command", required=True)

    health = commands.add_parser("health", help="检查 Supervisor，并在必要时恢复常驻进程")
    health.add_argument("--db", default=str(DEFAULT_DB))
    health.add_argument("--config", default=str(CONFIG_PATH))

    serve = commands.add_parser("serve", help="常驻运行 Supervisor 主进程")
    serve.add_argument("--db", default=str(DEFAULT_DB))
    serve.add_argument("--config", default=str(CONFIG_PATH))
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument(
        "--monitor-interval-seconds",
        type=float,
        default=30.0,
        help="Supervisor 组件状态检查周期（至少 1 秒）",
    )
    return value


def command_arguments(args: argparse.Namespace) -> list[str]:
    """把入口参数转为下层实现使用的稳定参数列表。"""
    values = ["--db", str(args.db), "--config", str(args.config)]
    if args.command == "serve":
        if args.host is not None:
            values.extend(["--host", str(args.host)])
        if args.port is not None:
            values.extend(["--port", str(args.port)])
    return values


def _count(database_path: Path, query: str, parameters: tuple[object, ...] = ()) -> int:
    """以短连接读取一个计数，Supervisor 永不直接变更任务数据库。"""
    database = connect(database_path)
    try:
        row = database.execute(query, parameters).fetchone()
        return int(row[0]) if row is not None else 0
    finally:
        database.close()


def _dispatcher_process_count() -> int | None:
    """返回当前运行的 Dispatcher 进程数；非 Windows 或查询失败时返回未知。"""
    if os.name != "nt":
        return None
    command = (
        "$self = $PID; (Get-CimInstance Win32_Process | Where-Object "
        "{ $_.ProcessId -ne $self -and $_.CommandLine -like '*codex_cli_dispatcher.py*' }).Count"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=5,
        )
        if completed.returncode != 0:
            return None
        return int(completed.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _scheduled_task_registered(task_name: str) -> bool | None:
    """检查 Dispatcher 的 Windows 计划任务是否已注册，不创建或修改计划任务。"""
    if os.name != "nt":
        return None
    try:
        completed = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", task_name],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=5,
        )
        return completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return None


def _monitored_count(database_path: Path, query: str) -> int | None:
    """读取监控计数；任务库短暂不可用时保留 Dashboard 服务。"""
    try:
        return _count(database_path, query)
    except Exception:
        return None


def component_snapshot(
    database_path: Path,
    config: dict[str, Any],
    *,
    dashboard_running: bool,
    dashboard_error: str | None,
) -> dict[str, dict[str, Any]]:
    """生成三个组件的可验证快照，不把外部自动化的未知状态伪装为正常。"""
    checked_at = now_shanghai()
    dashboard = {
        "component": "dashboard-server",
        "status": "HEALTHY" if dashboard_running else "RESTARTING",
        "checked_at": checked_at,
        "message": "Dashboard 服务线程正在运行。" if dashboard_running else "Dashboard 服务线程已退出，Supervisor 正在重启。",
    }
    if dashboard_error:
        dashboard["last_error"] = dashboard_error

    planner_config = ((config.get("automations") or {}).get("planner") or {})
    planner_scheduled = planner_config.get("scheduled") is True
    planner_pending = _monitored_count(
        database_path,
        "SELECT count(*) FROM tasks WHERE status='DRAFT' AND preflight_status='UNINSPECTED'",
    )
    planner_active = _monitored_count(
        database_path,
        "SELECT count(*) FROM tasks WHERE preflight_status='INSPECTING'",
    )
    if planner_pending is None or planner_active is None:
        planner_status = "UNAVAILABLE"
        planner_message = "无法读取任务数据库，无法判断 Planner 是否有待处理工作。"
    elif not planner_scheduled:
        planner_status = "DISABLED"
        planner_message = "Planner 自动化在初始化配置中未启用。"
    elif planner_active:
        planner_status = "RUNNING"
        planner_message = "任务数据库显示存在进行中的 Planner 预检。"
    elif planner_pending:
        planner_status = "PENDING_WORK"
        planner_message = "存在待预检草稿；Codex 自动化运行状态无法由本机 Supervisor 直接查询。"
    else:
        planner_status = "IDLE"
        planner_message = "没有待预检草稿；Codex 自动化运行状态无法由本机 Supervisor 直接查询。"
    planner = {
        "component": "planner",
        "status": planner_status,
        "checked_at": checked_at,
        "pending_tasks": planner_pending,
        "active_preflights": planner_active,
        "message": planner_message,
    }

    dispatcher_config = ((config.get("codex_cli") or {}).get("dispatcher") or {})
    task_name = str(dispatcher_config.get("task_name") or "")
    dispatcher_pending = _monitored_count(
        database_path,
        "SELECT count(*) FROM tasks WHERE status='PENDING' AND preflight_status='READY' "
        "AND runtime_environment='codex_cli' AND execution_policy='automatic'",
    )
    registered = _scheduled_task_registered(task_name) if task_name else None
    processes = _dispatcher_process_count()
    if dispatcher_pending is None:
        dispatcher_status = "UNAVAILABLE"
        dispatcher_message = "无法读取任务数据库，无法判断 Dispatcher 是否有待执行任务。"
    elif processes is not None and processes > 0:
        dispatcher_status = "RUNNING"
        dispatcher_message = "检测到正在运行的 Codex CLI Dispatcher 进程。"
    elif registered is True:
        dispatcher_status = "SCHEDULED_IDLE"
        dispatcher_message = "Dispatcher 计划任务已注册，当前没有运行中的 Dispatcher 进程。"
    elif registered is False:
        dispatcher_status = "UNDEPLOYED"
        dispatcher_message = "Dispatcher 计划任务尚未注册，无法自动调度待执行任务。"
    else:
        dispatcher_status = "UNOBSERVABLE"
        dispatcher_message = "当前系统无法查询 Dispatcher 计划任务状态。"
    dispatcher = {
        "component": "codex-cli-dispatcher",
        "status": dispatcher_status,
        "checked_at": checked_at,
        "pending_tasks": dispatcher_pending,
        "task_name": task_name or None,
        "message": dispatcher_message,
    }
    if registered is not None:
        dispatcher["scheduled_task_registered"] = registered
    if processes is not None:
        dispatcher["active_processes"] = processes
    return {"dashboard": dashboard, "planner": planner, "dispatcher": dispatcher}


class DashboardThread:
    """在 Supervisor 进程内启动、停止和重启 Dashboard HTTP 服务。"""

    def __init__(self, arguments: list[str]) -> None:
        self.arguments = arguments
        self.shutdown_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.shutdown_event = threading.Event()

        def target() -> None:
            try:
                dashboard_server.main(
                    self.arguments,
                    install_signal_handlers=False,
                    shutdown_event=self.shutdown_event,
                )
            except Exception as error:
                self.last_error = type(error).__name__

        self.thread = threading.Thread(target=target, name="dashboard-server", daemon=True)
        self.thread.start()

    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def stop(self) -> None:
        self.shutdown_event.set()
        if self.thread is not None:
            self.thread.join(timeout=10)


def serve_supervisor(args: argparse.Namespace) -> None:
    """托管 Dashboard 并周期检查多个组件，直到接到进程终止信号。"""
    if args.monitor_interval_seconds < 1:
        raise SystemExit("--monitor-interval-seconds 必须至少为 1")
    config_path = Path(args.config).resolve()
    database_path = Path(args.db).resolve()
    config = load_initialization_config(config_path)
    shutdown_event = threading.Event()
    dashboard = DashboardThread(command_arguments(args))

    def stop(signum: int, frame: object) -> None:
        del signum, frame
        shutdown_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    dashboard.start()
    print(f"{now_shanghai()} supervisor monitoring started", flush=True)
    try:
        while not shutdown_event.is_set():
            if not dashboard.is_running():
                dashboard.start()
            monitors = component_snapshot(
                database_path,
                config,
                dashboard_running=dashboard.is_running(),
                dashboard_error=dashboard.last_error,
            )
            health_run.record_monitor_state(monitors)
            shutdown_event.wait(args.monitor_interval_seconds)
    finally:
        dashboard.stop()


def main(argv: Sequence[str] | None = None) -> None:
    """执行一次命令分发；serve 持续运行，health 完成检查后立即退出。"""
    args = parser().parse_args(argv)
    values = command_arguments(args)
    if args.command == "health":
        health_run.main(values)
        return
    serve_supervisor(args)


if __name__ == "__main__":
    main()
