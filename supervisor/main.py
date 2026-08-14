"""仅支持 Windows 的 Supervisor 常驻宿主。

``serve`` 托管 Dashboard，并根据初始化配置管理 Planner Scheduler 与 Dispatcher
Scheduler。Supervisor 只核对常驻组件的 PID、进程身份和 heartbeat，不读取任务库，
也不根据任务数量推断调度器状态。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence


# 入口文件的位置是项目根目录的稳定依据，不受计划任务工作目录影响。
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = REPOSITORY_ROOT / "control"

# 直接运行本文件时，控制面模块和项目包都需要显式加入模块搜索路径。
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

# 配置路径、数据库路径和时间格式统一复用控制面的权威定义。
from loopdb import CONFIG_PATH, DEFAULT_DB, load_initialization_config, now_shanghai
from client import dashboard_server


# Supervisor 自己的运行文件与被托管组件的运行文件都集中在 runtime 目录。
RUNTIME_DIR = REPOSITORY_ROOT / "runtime"
HEALTH_STATE = RUNTIME_DIR / "health-state.json"
PID_PATH = RUNTIME_DIR / "supervisor.pid"
HEARTBEAT_PATH = RUNTIME_DIR / "supervisor-heartbeat.json"
FALLBACK_LOG = RUNTIME_DIR / "health-fallback.log"


@dataclass(frozen=True)
class ComponentSpec:
    """冻结一个 Scheduler 的进程入口、运行文件和监控阈值。"""

    key: str
    display_name: str
    enabled: bool
    entry: Path
    pid_path: Path
    heartbeat_path: Path
    stop_path: Path
    log_path: Path
    heartbeat_timeout_seconds: float


def read_monitor_state() -> dict[str, Any]:
    """读取健康状态；文件缺失或损坏时返回安全初始值。"""
    if not HEALTH_STATE.exists():
        return {"consecutive_failures": 0, "last_checked_at": None, "events": []}
    try:
        return json.loads(HEALTH_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"consecutive_failures": 0, "last_checked_at": None, "events": []}


def write_monitor_state(value: dict[str, Any]) -> None:
    """原子替换健康状态，避免进程中断留下不完整 JSON。"""
    temporary = HEALTH_STATE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(HEALTH_STATE)


def append_monitor_fallback(message: str) -> None:
    """组件快照无法保存时追加最小诊断，不中断主循环。"""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with FALLBACK_LOG.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(f"{now_shanghai()} {message}\n")


def record_monitor_state(monitors: dict[str, dict[str, Any]]) -> None:
    """保存组件快照，同时保留 health_run.py 拥有的健康检查字段。"""
    try:
        state = read_monitor_state()
        state["monitors"] = monitors
        state["monitor_checked_at"] = now_shanghai()
        write_monitor_state(state)
    except Exception as error:
        # 快照展示失败不能阻止 Supervisor 继续管理常驻组件。
        append_monitor_fallback(f"MONITOR_STATE_WRITE_FAILED {type(error).__name__}")


def write_supervisor_heartbeat(pid: int) -> None:
    """一轮组件检查结束后刷新 Supervisor heartbeat。"""
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


def component_specs(config: dict[str, Any]) -> tuple[ComponentSpec, ComponentSpec]:
    """从已校验配置构造两个 Scheduler 的稳定运行契约。"""
    components = config["supervisor"]["components"]

    def build(key: str, display_name: str, enabled: bool) -> ComponentSpec:
        raw = components[key]
        return ComponentSpec(
            key=key,
            display_name=display_name,
            enabled=enabled,
            entry=(REPOSITORY_ROOT / raw["entry"]).resolve(),
            pid_path=(REPOSITORY_ROOT / raw["pid_path"]).resolve(),
            heartbeat_path=(REPOSITORY_ROOT / raw["heartbeat_path"]).resolve(),
            stop_path=(REPOSITORY_ROOT / raw["stop_path"]).resolve(),
            log_path=(REPOSITORY_ROOT / raw["log_path"]).resolve(),
            heartbeat_timeout_seconds=float(raw["heartbeat_timeout_seconds"]),
        )

    planner = build(
        "planner",
        "Planner Scheduler",
        config["automations"]["planner"]["scheduled"] is True,
    )
    dispatcher = build(
        "dispatcher",
        "Dispatcher Scheduler",
        config["codex_cli"]["dispatcher"]["scheduled"] is True,
    )
    return planner, dispatcher


def read_pid(path: Path) -> int | None:
    """读取 PID 文件；缺失、不可读或非正整数都视为没有可信记录。"""
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
        return pid if pid > 0 else None
    except (FileNotFoundError, OSError, ValueError):
        return None


def process_alive(pid: int) -> bool:
    """使用 Windows 进程退出码判断 PID 是否仍在运行。"""
    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def windows_powershell(command: str) -> str:
    """隐藏执行只读 PowerShell 查询；失败或超时时返回空字符串。"""
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
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def is_component_process(pid: int, spec: ComponentSpec) -> bool:
    """核对 PID 存活，且命令行明确指向对应入口的 serve 子命令。"""
    if not process_alive(pid):
        return False
    command_line = windows_powershell(
        f'(Get-CimInstance Win32_Process -Filter "ProcessId = {pid}").CommandLine'
    )
    if not command_line:
        return False
    has_entry = str(spec.entry).casefold() in command_line.casefold()
    has_serve = re.search(r"(?:^|\s)serve(?:\s|$)", command_line, re.IGNORECASE) is not None
    return has_entry and has_serve


def read_component_heartbeat(path: Path) -> dict[str, Any] | None:
    """读取 Scheduler heartbeat，并把关键字段转换为稳定类型。"""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return None
        return {
            "component": str(value["component"]),
            "pid": int(value["pid"]),
            "status": str(value["status"]),
            "checked_at": str(value["checked_at"]),
        }
    except (FileNotFoundError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def component_health(
    spec: ComponentSpec,
    expected_pid: int | None = None,
) -> tuple[int | None, dict[str, Any] | None, str]:
    """联合 PID、heartbeat 和命令行身份判断 Scheduler 是否可信。"""
    pid = read_pid(spec.pid_path)
    if pid is None:
        return None, None, "PID 文件缺失或无效。"
    if expected_pid is not None and pid != expected_pid:
        return None, None, "PID 文件不属于刚启动的进程。"
    heartbeat = read_component_heartbeat(spec.heartbeat_path)
    if heartbeat is None:
        return None, None, "heartbeat 文件缺失或无效。"
    if heartbeat["component"] != spec.key or heartbeat["pid"] != pid:
        return None, heartbeat, "heartbeat 与组件身份或 PID 不一致。"
    if heartbeat["status"] != "RUNNING":
        return None, heartbeat, "heartbeat 未声明 RUNNING 状态。"
    try:
        checked_at = datetime.fromisoformat(heartbeat["checked_at"])
        current = datetime.fromisoformat(now_shanghai())
        if checked_at.tzinfo is None or current.tzinfo is None:
            return None, heartbeat, "heartbeat 时间缺少时区。"
    except (TypeError, ValueError):
        return None, heartbeat, "heartbeat 时间格式无效。"
    age = (current - checked_at).total_seconds()
    if age < 0 or age > spec.heartbeat_timeout_seconds:
        return None, heartbeat, "heartbeat 已超时或时间位于未来。"
    if not is_component_process(pid, spec):
        return None, heartbeat, "PID 对应进程不存在或身份不匹配。"
    return pid, heartbeat, "Scheduler 正常运行。"


def remove_runtime_files(spec: ComponentSpec, pid: int | None) -> None:
    """只清理已停止实例拥有的运行文件，避免删除新实例状态。"""
    recorded = read_pid(spec.pid_path)
    if pid is None or recorded == pid:
        spec.pid_path.unlink(missing_ok=True)
    heartbeat = read_component_heartbeat(spec.heartbeat_path)
    if pid is None or (heartbeat is not None and heartbeat["pid"] == pid):
        spec.heartbeat_path.unlink(missing_ok=True)
    spec.stop_path.unlink(missing_ok=True)


def request_component_stop(
    spec: ComponentSpec,
    pid: int,
    timeout_seconds: float,
    *,
    force_after_timeout: bool,
) -> bool:
    """向可信 Scheduler 请求正常退出，恢复场景超时后才终止 Scheduler 进程。"""
    if not process_alive(pid):
        remove_runtime_files(spec, pid)
        return True
    if not is_component_process(pid, spec):
        raise RuntimeError("PID 对应进程身份无法确认，拒绝发送停止请求。")
    request = {"component": spec.key, "pid": pid, "requested_at": now_shanghai()}
    temporary = spec.stop_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(request, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(spec.stop_path)
    deadline = time.monotonic() + timeout_seconds
    while process_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if process_alive(pid) and force_after_timeout:
        # 强制终止前再次核对身份，防止等待期间 PID 被系统复用。
        if not is_component_process(pid, spec):
            raise RuntimeError("停止等待后进程身份发生变化，拒绝强制终止。")
        os.kill(pid, signal.SIGTERM)
        force_deadline = time.monotonic() + timeout_seconds
        while process_alive(pid) and time.monotonic() < force_deadline:
            time.sleep(0.1)
    if process_alive(pid):
        return False
    remove_runtime_files(spec, pid)
    return True


def start_component(
    spec: ComponentSpec,
    database_path: Path,
    config_path: Path,
) -> subprocess.Popen[str]:
    """以隐藏的独立后台进程启动 Scheduler，并把输出追加到其专用日志。"""
    spec.log_path.parent.mkdir(parents=True, exist_ok=True)
    spec.stop_path.unlink(missing_ok=True)
    log_stream = spec.log_path.open("a", encoding="utf-8", newline="\n")
    creation_flags = (
        subprocess.CREATE_NEW_PROCESS_GROUP
        | subprocess.DETACHED_PROCESS
        | subprocess.CREATE_NO_WINDOW
    )
    try:
        return subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(spec.entry),
                "serve",
                "--db",
                str(database_path),
                "--config",
                str(config_path),
            ],
            cwd=str(REPOSITORY_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log_stream,
            stderr=log_stream,
            text=True,
            encoding="utf-8",
            close_fds=True,
            creationflags=creation_flags,
        )
    finally:
        # Popen 已复制日志句柄，Supervisor 不长期占用该文件描述符。
        log_stream.close()


def wait_component_started(
    spec: ComponentSpec,
    process: subprocess.Popen[str],
    timeout_seconds: float,
) -> tuple[int | None, dict[str, Any] | None, str]:
    """等待新进程写出与其 PID 匹配的新鲜 heartbeat。"""
    deadline = time.monotonic() + timeout_seconds
    last_reason = "Scheduler 尚未写入 heartbeat。"
    while time.monotonic() < deadline:
        pid, heartbeat, last_reason = component_health(spec, expected_pid=process.pid)
        if pid is not None:
            return pid, heartbeat, last_reason
        if process.poll() is not None:
            return None, heartbeat, f"Scheduler 启动后退出，退出码 {process.returncode}。"
        time.sleep(0.2)
    return None, read_component_heartbeat(spec.heartbeat_path), last_reason


def ensure_component(
    spec: ComponentSpec,
    database_path: Path,
    config_path: Path,
    start_timeout_seconds: float,
    stop_timeout_seconds: float,
) -> dict[str, Any]:
    """按配置开关确保 Scheduler 已停止或处于健康运行状态。"""
    checked_at = now_shanghai()
    recorded = read_pid(spec.pid_path)

    if not spec.enabled:
        if recorded is None:
            remove_runtime_files(spec, None)
            return {
                "component": spec.key,
                "status": "DISABLED",
                "checked_at": checked_at,
                "message": f"{spec.display_name} 自动调度已关闭。",
            }
        try:
            stopped = request_component_stop(
                spec,
                recorded,
                stop_timeout_seconds,
                force_after_timeout=False,
            )
        except (OSError, RuntimeError) as error:
            return {
                "component": spec.key,
                "status": "STOP_FAILED",
                "checked_at": checked_at,
                "pid": recorded,
                "message": f"自动调度已关闭，但无法安全停止 {spec.display_name}：{error}",
            }
        return {
            "component": spec.key,
            "status": "DISABLED" if stopped else "STOPPING",
            "checked_at": checked_at,
            "pid": None if stopped else recorded,
            "message": (
                f"{spec.display_name} 自动调度已关闭。"
                if stopped
                else f"{spec.display_name} 已收到停止请求；已启动的 Runner 不受影响。"
            ),
        }

    healthy_pid, heartbeat, reason = component_health(spec)
    if healthy_pid is not None:
        return {
            "component": spec.key,
            "status": "HEALTHY",
            "checked_at": checked_at,
            "pid": healthy_pid,
            "heartbeat": heartbeat,
            "message": f"{spec.display_name} 正常运行。",
        }

    if recorded is not None and process_alive(recorded):
        if not is_component_process(recorded, spec):
            return {
                "component": spec.key,
                "status": "BLOCKED",
                "checked_at": checked_at,
                "pid": recorded,
                "message": f"PID 文件指向无法确认身份的活动进程，拒绝恢复：{reason}",
            }
        try:
            stopped = request_component_stop(
                spec,
                recorded,
                stop_timeout_seconds,
                force_after_timeout=True,
            )
        except (OSError, RuntimeError) as error:
            return {
                "component": spec.key,
                "status": "UNHEALTHY",
                "checked_at": checked_at,
                "pid": recorded,
                "message": f"{spec.display_name} 不健康且无法安全停止：{error}",
            }
        if not stopped:
            return {
                "component": spec.key,
                "status": "UNHEALTHY",
                "checked_at": checked_at,
                "pid": recorded,
                "message": f"{spec.display_name} 不健康且未在停止期限内退出。",
            }
    else:
        # PID 无效或进程已结束时，旧运行文件不应阻挡新实例创建。
        remove_runtime_files(spec, recorded)

    try:
        process = start_component(spec, database_path, config_path)
        recovered_pid, recovered_heartbeat, start_reason = wait_component_started(
            spec,
            process,
            start_timeout_seconds,
        )
    except OSError as error:
        return {
            "component": spec.key,
            "status": "UNHEALTHY",
            "checked_at": checked_at,
            "message": f"{spec.display_name} 启动失败：{error}",
        }
    if recovered_pid is None:
        return {
            "component": spec.key,
            "status": "UNHEALTHY",
            "checked_at": checked_at,
            "pid": process.pid,
            "message": f"{spec.display_name} 启动后未通过探活：{start_reason}",
        }
    return {
        "component": spec.key,
        "status": "RESTARTED",
        "checked_at": checked_at,
        "pid": recovered_pid,
        "heartbeat": recovered_heartbeat,
        "message": f"{spec.display_name} 已启动或恢复。",
    }


def component_snapshot(
    specs: tuple[ComponentSpec, ComponentSpec],
    database_path: Path,
    config_path: Path,
    *,
    dashboard_running: bool,
    dashboard_error: str | None,
    start_timeout_seconds: float,
    stop_timeout_seconds: float,
) -> dict[str, dict[str, Any]]:
    """管理三个常驻组件并返回本轮可验证状态。"""
    checked_at = now_shanghai()
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

    def inspect(spec: ComponentSpec) -> dict[str, Any]:
        """把单个组件的意外监控异常限制在本轮状态中。"""
        try:
            return ensure_component(
                spec,
                database_path,
                config_path,
                start_timeout_seconds,
                stop_timeout_seconds,
            )
        except Exception as error:
            return {
                "component": spec.key,
                "status": "UNHEALTHY",
                "checked_at": now_shanghai(),
                "message": f"{spec.display_name} 监控失败：{type(error).__name__}",
            }

    planner, dispatcher = (inspect(spec) for spec in specs)
    return {"dashboard": dashboard, "planner": planner, "dispatcher": dispatcher}


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
            # Dashboard 线程意外退出时，下一轮先恢复服务再记录状态。
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
            # Event.wait 可被终止信号提前唤醒，不会强制睡满整个周期。
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
