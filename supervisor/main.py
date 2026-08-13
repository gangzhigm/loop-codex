"""仅支持 Windows 的 Supervisor 常驻宿主。

``serve`` 是长期运行的 Supervisor 主进程：它托管本机 Dashboard，并周期性检查
Dashboard、Planner 与 Codex CLI Dispatcher 的可核实状态。它只读取任务投影和本机
进程/计划任务信息，不领取任务、不管理 Codex 自动化，也不直接写 SQLite。

健康检查由 ``health_run.py`` 独立执行，用于探测和恢复整个 ``serve`` 进程。
"""

from __future__ import annotations

# 本文件只承载常驻 Supervisor，任务状态写入仍只能经过 control/loopctl.py。
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Sequence


# 从当前入口文件反推项目根目录，避免依赖启动命令所在目录。
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = REPOSITORY_ROOT / "control"

# loopdb 位于 control 目录；直接运行 main.py 时需要显式加入模块搜索路径。
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))

# client 是项目根目录下的包，直接运行脚本时同样需要项目根目录可导入。
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

# 统一复用控制面的配置加载、数据库连接和上海时区时间函数。
from loopdb import CONFIG_PATH, DEFAULT_DB, connect, load_initialization_config, now_shanghai

# 根据当前启动方式导入项目根目录的 Dashboard 模块。
if __package__:
    from client import dashboard_server
else:
    from client import dashboard_server


# 常驻 Supervisor 自己维护 PID、heartbeat 和组件监控快照。
RUNTIME_DIR = REPOSITORY_ROOT / "runtime"
HEALTH_STATE = RUNTIME_DIR / "health-state.json"
PID_PATH = RUNTIME_DIR / "supervisor.pid"
HEARTBEAT_PATH = RUNTIME_DIR / "supervisor-heartbeat.json"
FALLBACK_LOG = RUNTIME_DIR / "health-fallback.log"


def read_monitor_state() -> dict[str, Any]:
    """读取健康状态；文件缺失或损坏时返回不含历史记录的初始值。"""
    if not HEALTH_STATE.exists():
        return {"consecutive_failures": 0, "last_checked_at": None, "events": []}
    try:
        return json.loads(HEALTH_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"consecutive_failures": 0, "last_checked_at": None, "events": []}


def write_monitor_state(value: dict[str, Any]) -> None:
    """原子替换健康状态，避免 Supervisor 中断时留下半截 JSON。"""
    temporary = HEALTH_STATE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(HEALTH_STATE)


def append_monitor_fallback(message: str) -> None:
    """组件快照无法保存时记录最小诊断，不中断 Supervisor 主循环。"""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with FALLBACK_LOG.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(f"{now_shanghai()} {message}\n")


def record_monitor_state(monitors: dict[str, dict[str, Any]]) -> None:
    """保存本轮组件监控快照，同时保留外部健康检查写入的字段。"""
    try:
        state = read_monitor_state()
        state["monitors"] = monitors
        state["monitor_checked_at"] = now_shanghai()
        write_monitor_state(state)
    except Exception as error:
        # 快照展示失败不能阻止 Supervisor 继续监控和恢复各组件。
        append_monitor_fallback(f"MONITOR_STATE_WRITE_FAILED {type(error).__name__}")


def write_supervisor_heartbeat(pid: int) -> None:
    """主循环完成一轮监控后刷新 heartbeat，供外部健康任务探活。"""
    value = {"pid": pid, "checked_at": now_shanghai()}
    temporary = HEARTBEAT_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(HEARTBEAT_PATH)


def heartbeat_belongs_to(pid: int) -> bool:
    """退出清理前确认 heartbeat 仍属于当前 Supervisor 实例。"""
    try:
        value = json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))
        return isinstance(value, dict) and int(value["pid"]) == pid
    except (FileNotFoundError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False


def parser() -> argparse.ArgumentParser:
    """创建只用于启动常驻 Supervisor 的命令行。"""
    value = argparse.ArgumentParser(description="Local Agent Loop Supervisor entry point")

    # 固定使用 serve 子命令，避免把健康检查逻辑重新放回常驻入口。
    commands = value.add_subparsers(dest="command", required=True)
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
    """把入口参数转为 Dashboard 下层实现使用的稳定参数列表。"""
    values = ["--db", str(args.db), "--config", str(args.config)]

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
        # 每次监控查询都关闭连接，避免常驻进程长期占用 SQLite 资源。
        database.close()


def _dispatcher_process_count() -> int | None:
    """返回当前运行的 Dispatcher 进程数；查询失败时返回未知。"""
    # 排除执行查询的 PowerShell 自身，避免命令行中的脚本名造成误报。
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
        # 进程查询失败表示状态未知，不能据此声称 Dispatcher 已停止。
        return None


def _scheduled_task_registered(task_name: str) -> bool | None:
    """检查 Dispatcher 的 Windows 计划任务是否已注册，不创建或修改计划任务。"""
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
        # 系统命令不可用时保留“无法观察”，不误判为“未部署”。
        return None


def _monitored_count(database_path: Path, query: str) -> int | None:
    """读取监控计数；任务库短暂不可用时返回未知而不中断 Dashboard。"""
    try:
        return _count(database_path, query)
    except Exception:
        # 监控读取失败不得拖垮常驻服务，具体异常由状态 UNAVAILABLE 表达。
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

    # Dashboard 状态来自本进程所托管线程，能够直接判断运行或重启中。
    dashboard = {
        "component": "dashboard-server",
        "status": "HEALTHY" if dashboard_running else "RESTARTING",
        "checked_at": checked_at,
        "message": (
            "Dashboard 服务线程正在运行。"
            if dashboard_running
            else "Dashboard 服务线程已退出，Supervisor 正在重启。"
        ),
    }
    if dashboard_error:
        dashboard["last_error"] = dashboard_error

    # Planner 是外部 Codex 自动化，因此这里只结合配置和任务库判断是否有工作。
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

    # 按证据强弱排序：数据库不可读优先于配置和任务数量判断。
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

    # 对外保留判断依据，便于 Dashboard 展示待处理数和活动预检数。
    planner = {
        "component": "planner",
        "status": planner_status,
        "checked_at": checked_at,
        "pending_tasks": planner_pending,
        "active_preflights": planner_active,
        "message": planner_message,
    }

    # Dispatcher 同时检查任务需求、计划任务部署状态和瞬时进程状态。
    dispatcher_config = ((config.get("codex_cli") or {}).get("dispatcher") or {})
    task_name = str(dispatcher_config.get("task_name") or "")
    dispatcher_pending = _monitored_count(
        database_path,
        "SELECT count(*) FROM tasks WHERE status='PENDING' AND preflight_status='READY' "
        "AND runtime_environment='codex_cli' AND execution_policy='automatic'",
    )
    registered = _scheduled_task_registered(task_name) if task_name else None
    processes = _dispatcher_process_count()

    # 正在运行的进程证据最直接；没有进程时再区分已调度空闲和未部署。
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

    # 固定键名作为健康状态文件和 Dashboard 的稳定契约。
    return {"dashboard": dashboard, "planner": planner, "dispatcher": dispatcher}


class DashboardThread:
    """在 Supervisor 进程内启动、停止和重启 Dashboard HTTP 服务。"""

    def __init__(self, arguments: list[str]) -> None:
        # 保存 Dashboard 参数，线程异常退出后的下一次启动仍复用同一配置。
        self.arguments = arguments
        self.shutdown_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        # 已有活动线程时保持幂等，避免重复绑定同一端口。
        if self.thread is not None and self.thread.is_alive():
            return

        # 每次重启都创建新的事件，旧事件可能已处于触发状态。
        self.shutdown_event = threading.Event()

        def target() -> None:
            try:
                dashboard_server.main(
                    self.arguments,
                    install_signal_handlers=False,
                    shutdown_event=self.shutdown_event,
                )
            except Exception as error:
                # 健康快照只记录异常类型，避免把潜在敏感上下文写入长期状态。
                self.last_error = type(error).__name__

        # 使用守护线程，使 Supervisor 主进程退出时不会被残留 HTTP 线程阻塞。
        self.thread = threading.Thread(target=target, name="dashboard-server", daemon=True)
        self.thread.start()

    def is_running(self) -> bool:
        """判断 Dashboard 托管线程是否仍在运行。"""
        return self.thread is not None and self.thread.is_alive()

    def stop(self) -> None:
        """通知 Dashboard 关闭，并给服务线程最多十秒完成清理。"""
        self.shutdown_event.set()
        if self.thread is not None:
            self.thread.join(timeout=10)


def serve_supervisor(args: argparse.Namespace) -> None:
    """托管 Dashboard 并周期检查多个组件，直到接到进程终止信号。"""
    if args.monitor_interval_seconds < 1:
        raise SystemExit("--monitor-interval-seconds 必须至少为 1")

    # 启动时冻结配置和数据库路径；配置变更通过重启 Supervisor 生效。
    config_path = Path(args.config).resolve()
    database_path = Path(args.db).resolve()
    config = load_initialization_config(config_path)
    shutdown_event = threading.Event()
    dashboard = DashboardThread(command_arguments(args))
    pid = os.getpid()
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            PID_PATH,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        )
    except FileExistsError:
        raise SystemExit(
            "Supervisor PID 文件已存在；请通过 health 命令检查或恢复现有主进程"
        ) from None
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(str(pid))

    def stop(signum: int, frame: object) -> None:
        # 信号处理器只设置事件，实际资源清理由主循环 finally 完成。
        del signum, frame
        shutdown_event.set()

    try:
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        dashboard.start()
        print(f"{now_shanghai()} supervisor monitoring started", flush=True)

        while not shutdown_event.is_set():
            # Dashboard 线程意外退出时，在下一轮监控开始前立即重新拉起。
            if not dashboard.is_running():
                dashboard.start()

            monitors = component_snapshot(
                database_path,
                config,
                dashboard_running=dashboard.is_running(),
                dashboard_error=dashboard.last_error,
            )
            record_monitor_state(monitors)
            write_supervisor_heartbeat(pid)

            # 使用事件等待，使终止信号可以提前唤醒主循环，而不是固定休眠。
            shutdown_event.wait(args.monitor_interval_seconds)
    finally:
        dashboard.stop()
        if (
            PID_PATH.exists()
            and PID_PATH.read_text(encoding="utf-8").strip() == str(pid)
        ):
            PID_PATH.unlink()
        if heartbeat_belongs_to(pid):
            HEARTBEAT_PATH.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> None:
    """解析 serve 参数并启动长期运行的 Supervisor。"""
    args = parser().parse_args(argv)
    serve_supervisor(args)


if __name__ == "__main__":
    main()
