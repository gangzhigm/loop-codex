"""Supervisor 的常驻宿主与一次性健康检查入口。

``serve`` 是长期运行的 Supervisor 主进程：它托管本机 Dashboard，并周期性检查
Dashboard、Planner 与 Codex CLI Dispatcher 的可核实状态。它只读取任务投影和本机
进程/计划任务信息，不领取任务、不管理 Codex 自动化，也不直接写 SQLite。

``health`` 由 Windows 计划任务周期调用，用于探测和恢复整个 ``serve`` 进程。
"""

# 说明下一条语句的作用。
from __future__ import annotations

# 中文排查：计划任务调用 health；由 health 恢复的常驻主进程调用 serve。
# 本文件只组织 Supervisor 命令边界，任务状态写入仍只能经过 scripts/loopctl.py。

# 说明下一条语句的作用。
import argparse
# 说明下一条语句的作用。
import os
# 说明下一条语句的作用。
import signal
# 说明下一条语句的作用。
import subprocess
# 说明下一条语句的作用。
import sys
# 说明下一条语句的作用。
import threading
# 说明下一条语句的作用。
import time
# 说明下一条语句的作用。
from pathlib import Path
# 说明下一条语句的作用。
from typing import Any, Sequence


# 说明下一条语句的作用。
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
# 说明下一条语句的作用。
SCRIPTS_ROOT = REPOSITORY_ROOT / "scripts"
# 说明下一条语句的作用。
if str(SCRIPTS_ROOT) not in sys.path:
    # 说明下一条语句的作用。
    sys.path.insert(0, str(SCRIPTS_ROOT))
# 说明下一条语句的作用。
if str(REPOSITORY_ROOT) not in sys.path:
    # 说明下一条语句的作用。
    sys.path.insert(0, str(REPOSITORY_ROOT))

# 说明下一条语句的作用。
from loopdb import CONFIG_PATH, DEFAULT_DB, connect, load_initialization_config, now_shanghai
# 说明下一条语句的作用。
if __package__:
    # 说明下一条语句的作用。
    from . import health_run
    # 说明下一条语句的作用。
    from client import dashboard_server
# 说明下一条语句的作用。
else:
    # 计划任务以 ``python supervisor/main.py`` 直接执行时，没有包上下文。
    # 说明下一条语句的作用。
    import health_run
    # 说明下一条语句的作用。
    from client import dashboard_server


# 说明下一条语句的作用。
def parser() -> argparse.ArgumentParser:
    """创建 Supervisor 命令行，明确区分短暂检查和常驻服务。"""
    # 说明下一条语句的作用。
    value = argparse.ArgumentParser(description="Local Agent Loop Supervisor entry point")
    # 说明下一条语句的作用。
    commands = value.add_subparsers(dest="command", required=True)

    # 说明下一条语句的作用。
    health = commands.add_parser("health", help="检查 Supervisor，并在必要时恢复常驻进程")
    # 说明下一条语句的作用。
    health.add_argument("--db", default=str(DEFAULT_DB))
    # 说明下一条语句的作用。
    health.add_argument("--config", default=str(CONFIG_PATH))

    # 说明下一条语句的作用。
    serve = commands.add_parser("serve", help="常驻运行 Supervisor 主进程")
    # 说明下一条语句的作用。
    serve.add_argument("--db", default=str(DEFAULT_DB))
    # 说明下一条语句的作用。
    serve.add_argument("--config", default=str(CONFIG_PATH))
    # 说明下一条语句的作用。
    serve.add_argument("--host")
    # 说明下一条语句的作用。
    serve.add_argument("--port", type=int)
    # 说明下一条语句的作用。
    serve.add_argument(
        # 说明下一条语句的作用。
        "--monitor-interval-seconds",
        # 说明下一条语句的作用。
        type=float,
        # 说明下一条语句的作用。
        default=30.0,
        # 说明下一条语句的作用。
        help="Supervisor 组件状态检查周期（至少 1 秒）",
    # 说明下一条语句的作用。
    )
    # 说明下一条语句的作用。
    return value


# 说明下一条语句的作用。
def command_arguments(args: argparse.Namespace) -> list[str]:
    """把入口参数转为下层实现使用的稳定参数列表。"""
    # 说明下一条语句的作用。
    values = ["--db", str(args.db), "--config", str(args.config)]
    # 说明下一条语句的作用。
    if args.command == "serve":
        # 说明下一条语句的作用。
        if args.host is not None:
            # 说明下一条语句的作用。
            values.extend(["--host", str(args.host)])
        # 说明下一条语句的作用。
        if args.port is not None:
            # 说明下一条语句的作用。
            values.extend(["--port", str(args.port)])
    # 说明下一条语句的作用。
    return values


# 说明下一条语句的作用。
def _count(database_path: Path, query: str, parameters: tuple[object, ...] = ()) -> int:
    """以短连接读取一个计数，Supervisor 永不直接变更任务数据库。"""
    # 说明下一条语句的作用。
    database = connect(database_path)
    # 说明下一条语句的作用。
    try:
        # 说明下一条语句的作用。
        row = database.execute(query, parameters).fetchone()
        # 说明下一条语句的作用。
        return int(row[0]) if row is not None else 0
    # 说明下一条语句的作用。
    finally:
        # 说明下一条语句的作用。
        database.close()


# 说明下一条语句的作用。
def _dispatcher_process_count() -> int | None:
    """返回当前运行的 Dispatcher 进程数；非 Windows 或查询失败时返回未知。"""
    # 说明下一条语句的作用。
    if os.name != "nt":
        # 说明下一条语句的作用。
        return None
    # 说明下一条语句的作用。
    command = (
        # 说明下一条语句的作用。
        "$self = $PID; (Get-CimInstance Win32_Process | Where-Object "
        # 说明下一条语句的作用。
        "{ $_.ProcessId -ne $self -and $_.CommandLine -like '*codex_cli_dispatcher.py*' }).Count"
    # 说明下一条语句的作用。
    )
    # 说明下一条语句的作用。
    try:
        # 说明下一条语句的作用。
        completed = subprocess.run(
            # 说明下一条语句的作用。
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            # 说明下一条语句的作用。
            check=False,
            # 说明下一条语句的作用。
            capture_output=True,
            # 说明下一条语句的作用。
            text=True,
            # 说明下一条语句的作用。
            encoding="utf-8",
            # 说明下一条语句的作用。
            errors="replace",
            # 说明下一条语句的作用。
            creationflags=subprocess.CREATE_NO_WINDOW,
            # 说明下一条语句的作用。
            timeout=5,
        # 说明下一条语句的作用。
        )
        # 说明下一条语句的作用。
        if completed.returncode != 0:
            # 说明下一条语句的作用。
            return None
        # 说明下一条语句的作用。
        return int(completed.stdout.strip())
    # 说明下一条语句的作用。
    except (OSError, ValueError, subprocess.TimeoutExpired):
        # 说明下一条语句的作用。
        return None


# 说明下一条语句的作用。
def _scheduled_task_registered(task_name: str) -> bool | None:
    """检查 Dispatcher 的 Windows 计划任务是否已注册，不创建或修改计划任务。"""
    # 说明下一条语句的作用。
    if os.name != "nt":
        # 说明下一条语句的作用。
        return None
    # 说明下一条语句的作用。
    try:
        # 说明下一条语句的作用。
        completed = subprocess.run(
            # 说明下一条语句的作用。
            ["schtasks.exe", "/Query", "/TN", task_name],
            # 说明下一条语句的作用。
            check=False,
            # 说明下一条语句的作用。
            capture_output=True,
            # 说明下一条语句的作用。
            text=True,
            # 说明下一条语句的作用。
            encoding="utf-8",
            # 说明下一条语句的作用。
            errors="replace",
            # 说明下一条语句的作用。
            creationflags=subprocess.CREATE_NO_WINDOW,
            # 说明下一条语句的作用。
            timeout=5,
        # 说明下一条语句的作用。
        )
        # 说明下一条语句的作用。
        return completed.returncode == 0
    # 说明下一条语句的作用。
    except (OSError, subprocess.TimeoutExpired):
        # 说明下一条语句的作用。
        return None


# 说明下一条语句的作用。
def _monitored_count(database_path: Path, query: str) -> int | None:
    """读取监控计数；任务库短暂不可用时保留 Dashboard 服务。"""
    # 说明下一条语句的作用。
    try:
        # 说明下一条语句的作用。
        return _count(database_path, query)
    # 说明下一条语句的作用。
    except Exception:
        # 说明下一条语句的作用。
        return None


# 说明下一条语句的作用。
def component_snapshot(
    # 说明下一条语句的作用。
    database_path: Path,
    # 说明下一条语句的作用。
    config: dict[str, Any],
    # 说明下一条语句的作用。
    *,
    # 说明下一条语句的作用。
    dashboard_running: bool,
    # 说明下一条语句的作用。
    dashboard_error: str | None,
# 说明下一条语句的作用。
) -> dict[str, dict[str, Any]]:
    """生成三个组件的可验证快照，不把外部自动化的未知状态伪装为正常。"""
    # 说明下一条语句的作用。
    checked_at = now_shanghai()
    # 说明下一条语句的作用。
    dashboard = {
        # 说明下一条语句的作用。
        "component": "dashboard-server",
        # 说明下一条语句的作用。
        "status": "HEALTHY" if dashboard_running else "RESTARTING",
        # 说明下一条语句的作用。
        "checked_at": checked_at,
        # 说明下一条语句的作用。
        "message": "Dashboard 服务线程正在运行。" if dashboard_running else "Dashboard 服务线程已退出，Supervisor 正在重启。",
    # 说明下一条语句的作用。
    }
    # 说明下一条语句的作用。
    if dashboard_error:
        # 说明下一条语句的作用。
        dashboard["last_error"] = dashboard_error

    # 说明下一条语句的作用。
    planner_config = ((config.get("automations") or {}).get("planner") or {})
    # 说明下一条语句的作用。
    planner_scheduled = planner_config.get("scheduled") is True
    # 说明下一条语句的作用。
    planner_pending = _monitored_count(
        # 说明下一条语句的作用。
        database_path,
        # 说明下一条语句的作用。
        "SELECT count(*) FROM tasks WHERE status='DRAFT' AND preflight_status='UNINSPECTED'",
    # 说明下一条语句的作用。
    )
    # 说明下一条语句的作用。
    planner_active = _monitored_count(
        # 说明下一条语句的作用。
        database_path,
        # 说明下一条语句的作用。
        "SELECT count(*) FROM tasks WHERE preflight_status='INSPECTING'",
    # 说明下一条语句的作用。
    )
    # 说明下一条语句的作用。
    if planner_pending is None or planner_active is None:
        # 说明下一条语句的作用。
        planner_status = "UNAVAILABLE"
        # 说明下一条语句的作用。
        planner_message = "无法读取任务数据库，无法判断 Planner 是否有待处理工作。"
    # 说明下一条语句的作用。
    elif not planner_scheduled:
        # 说明下一条语句的作用。
        planner_status = "DISABLED"
        # 说明下一条语句的作用。
        planner_message = "Planner 自动化在初始化配置中未启用。"
    # 说明下一条语句的作用。
    elif planner_active:
        # 说明下一条语句的作用。
        planner_status = "RUNNING"
        # 说明下一条语句的作用。
        planner_message = "任务数据库显示存在进行中的 Planner 预检。"
    # 说明下一条语句的作用。
    elif planner_pending:
        # 说明下一条语句的作用。
        planner_status = "PENDING_WORK"
        # 说明下一条语句的作用。
        planner_message = "存在待预检草稿；Codex 自动化运行状态无法由本机 Supervisor 直接查询。"
    # 说明下一条语句的作用。
    else:
        # 说明下一条语句的作用。
        planner_status = "IDLE"
        # 说明下一条语句的作用。
        planner_message = "没有待预检草稿；Codex 自动化运行状态无法由本机 Supervisor 直接查询。"
    # 说明下一条语句的作用。
    planner = {
        # 说明下一条语句的作用。
        "component": "planner",
        # 说明下一条语句的作用。
        "status": planner_status,
        # 说明下一条语句的作用。
        "checked_at": checked_at,
        # 说明下一条语句的作用。
        "pending_tasks": planner_pending,
        # 说明下一条语句的作用。
        "active_preflights": planner_active,
        # 说明下一条语句的作用。
        "message": planner_message,
    # 说明下一条语句的作用。
    }

    # 说明下一条语句的作用。
    dispatcher_config = ((config.get("codex_cli") or {}).get("dispatcher") or {})
    # 说明下一条语句的作用。
    task_name = str(dispatcher_config.get("task_name") or "")
    # 说明下一条语句的作用。
    dispatcher_pending = _monitored_count(
        # 说明下一条语句的作用。
        database_path,
        # 说明下一条语句的作用。
        "SELECT count(*) FROM tasks WHERE status='PENDING' AND preflight_status='READY' "
        # 说明下一条语句的作用。
        "AND runtime_environment='codex_cli' AND execution_policy='automatic'",
    # 说明下一条语句的作用。
    )
    # 说明下一条语句的作用。
    registered = _scheduled_task_registered(task_name) if task_name else None
    # 说明下一条语句的作用。
    processes = _dispatcher_process_count()
    # 说明下一条语句的作用。
    if dispatcher_pending is None:
        # 说明下一条语句的作用。
        dispatcher_status = "UNAVAILABLE"
        # 说明下一条语句的作用。
        dispatcher_message = "无法读取任务数据库，无法判断 Dispatcher 是否有待执行任务。"
    # 说明下一条语句的作用。
    elif processes is not None and processes > 0:
        # 说明下一条语句的作用。
        dispatcher_status = "RUNNING"
        # 说明下一条语句的作用。
        dispatcher_message = "检测到正在运行的 Codex CLI Dispatcher 进程。"
    # 说明下一条语句的作用。
    elif registered is True:
        # 说明下一条语句的作用。
        dispatcher_status = "SCHEDULED_IDLE"
        # 说明下一条语句的作用。
        dispatcher_message = "Dispatcher 计划任务已注册，当前没有运行中的 Dispatcher 进程。"
    # 说明下一条语句的作用。
    elif registered is False:
        # 说明下一条语句的作用。
        dispatcher_status = "UNDEPLOYED"
        # 说明下一条语句的作用。
        dispatcher_message = "Dispatcher 计划任务尚未注册，无法自动调度待执行任务。"
    # 说明下一条语句的作用。
    else:
        # 说明下一条语句的作用。
        dispatcher_status = "UNOBSERVABLE"
        # 说明下一条语句的作用。
        dispatcher_message = "当前系统无法查询 Dispatcher 计划任务状态。"
    # 说明下一条语句的作用。
    dispatcher = {
        # 说明下一条语句的作用。
        "component": "codex-cli-dispatcher",
        # 说明下一条语句的作用。
        "status": dispatcher_status,
        # 说明下一条语句的作用。
        "checked_at": checked_at,
        # 说明下一条语句的作用。
        "pending_tasks": dispatcher_pending,
        # 说明下一条语句的作用。
        "task_name": task_name or None,
        # 说明下一条语句的作用。
        "message": dispatcher_message,
    # 说明下一条语句的作用。
    }
    # 说明下一条语句的作用。
    if registered is not None:
        # 说明下一条语句的作用。
        dispatcher["scheduled_task_registered"] = registered
    # 说明下一条语句的作用。
    if processes is not None:
        # 说明下一条语句的作用。
        dispatcher["active_processes"] = processes
    # 说明下一条语句的作用。
    return {"dashboard": dashboard, "planner": planner, "dispatcher": dispatcher}


# 说明下一条语句的作用。
class DashboardThread:
    """在 Supervisor 进程内启动、停止和重启 Dashboard HTTP 服务。"""

    # 说明下一条语句的作用。
    def __init__(self, arguments: list[str]) -> None:
        # 说明下一条语句的作用。
        self.arguments = arguments
        # 说明下一条语句的作用。
        self.shutdown_event = threading.Event()
        # 说明下一条语句的作用。
        self.thread: threading.Thread | None = None
        # 说明下一条语句的作用。
        self.last_error: str | None = None

    # 说明下一条语句的作用。
    def start(self) -> None:
        # 说明下一条语句的作用。
        if self.thread is not None and self.thread.is_alive():
            # 说明下一条语句的作用。
            return
        # 说明下一条语句的作用。
        self.shutdown_event = threading.Event()

        # 说明下一条语句的作用。
        def target() -> None:
            # 说明下一条语句的作用。
            try:
                # 说明下一条语句的作用。
                dashboard_server.main(
                    # 说明下一条语句的作用。
                    self.arguments,
                    # 说明下一条语句的作用。
                    install_signal_handlers=False,
                    # 说明下一条语句的作用。
                    shutdown_event=self.shutdown_event,
                # 说明下一条语句的作用。
                )
            # 说明下一条语句的作用。
            except Exception as error:
                # 说明下一条语句的作用。
                self.last_error = type(error).__name__

        # 说明下一条语句的作用。
        self.thread = threading.Thread(target=target, name="dashboard-server", daemon=True)
        # 说明下一条语句的作用。
        self.thread.start()

    # 说明下一条语句的作用。
    def is_running(self) -> bool:
        # 说明下一条语句的作用。
        return self.thread is not None and self.thread.is_alive()

    # 说明下一条语句的作用。
    def stop(self) -> None:
        # 说明下一条语句的作用。
        self.shutdown_event.set()
        # 说明下一条语句的作用。
        if self.thread is not None:
            # 说明下一条语句的作用。
            self.thread.join(timeout=10)


# 说明下一条语句的作用。
def serve_supervisor(args: argparse.Namespace) -> None:
    """托管 Dashboard 并周期检查多个组件，直到接到进程终止信号。"""
    # 说明下一条语句的作用。
    if args.monitor_interval_seconds < 1:
        # 说明下一条语句的作用。
        raise SystemExit("--monitor-interval-seconds 必须至少为 1")
    # 说明下一条语句的作用。
    config_path = Path(args.config).resolve()
    # 说明下一条语句的作用。
    database_path = Path(args.db).resolve()
    # 说明下一条语句的作用。
    config = load_initialization_config(config_path)
    # 说明下一条语句的作用。
    shutdown_event = threading.Event()
    # 说明下一条语句的作用。
    dashboard = DashboardThread(command_arguments(args))

    # 说明下一条语句的作用。
    def stop(signum: int, frame: object) -> None:
        # 说明下一条语句的作用。
        del signum, frame
        # 说明下一条语句的作用。
        shutdown_event.set()

    # 说明下一条语句的作用。
    signal.signal(signal.SIGTERM, stop)
    # 说明下一条语句的作用。
    signal.signal(signal.SIGINT, stop)
    # 说明下一条语句的作用。
    dashboard.start()
    # 说明下一条语句的作用。
    print(f"{now_shanghai()} supervisor monitoring started", flush=True)
    # 说明下一条语句的作用。
    try:
        # 说明下一条语句的作用。
        while not shutdown_event.is_set():
            # 说明下一条语句的作用。
            if not dashboard.is_running():
                # 说明下一条语句的作用。
                dashboard.start()
            # 说明下一条语句的作用。
            monitors = component_snapshot(
                # 说明下一条语句的作用。
                database_path,
                # 说明下一条语句的作用。
                config,
                # 说明下一条语句的作用。
                dashboard_running=dashboard.is_running(),
                # 说明下一条语句的作用。
                dashboard_error=dashboard.last_error,
            # 说明下一条语句的作用。
            )
            # 说明下一条语句的作用。
            health_run.record_monitor_state(monitors)
            # 说明下一条语句的作用。
            shutdown_event.wait(args.monitor_interval_seconds)
    # 说明下一条语句的作用。
    finally:
        # 说明下一条语句的作用。
        dashboard.stop()


# 说明下一条语句的作用。
def main(argv: Sequence[str] | None = None) -> None:
    """执行一次命令分发；serve 持续运行，health 完成检查后立即退出。"""
    # 说明下一条语句的作用。
    args = parser().parse_args(argv)
    # 说明下一条语句的作用。
    values = command_arguments(args)
    # 说明下一条语句的作用。
    if args.command == "health":
        # 说明下一条语句的作用。
        health_run.main(values)
        # 说明下一条语句的作用。
        return
    # 说明下一条语句的作用。
    serve_supervisor(args)


# 说明下一条语句的作用。
if __name__ == "__main__":
    # 说明下一条语句的作用。
    main()
