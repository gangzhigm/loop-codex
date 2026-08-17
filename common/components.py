"""Supervisor 对 Planner、Dispatcher 和 Dashboard 的组件管理方法。"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from loopdb import now_shanghai

from common.files import (
    append_utf8_line,
    heartbeat_belongs_to as file_heartbeat_belongs_to,
    read_json_object,
    read_pid,
    write_json_atomic,
)
from common.paths import (
    FALLBACK_LOG,
    HEALTH_STATE,
    HEARTBEAT_PATH,
    REPOSITORY_ROOT,
)
from common.runners import runner_snapshot
from common.windows import process_alive, windows_powershell


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
    return read_json_object(HEALTH_STATE) or {
        "consecutive_failures": 0,
        "last_checked_at": None,
        "events": [],
    }


def write_monitor_state(value: dict[str, Any]) -> None:
    """原子替换健康状态，避免进程中断留下不完整 JSON。"""
    write_json_atomic(HEALTH_STATE, value)


def append_monitor_fallback(message: str) -> None:
    """组件快照无法保存时追加最小诊断，不中断主循环。"""
    append_utf8_line(FALLBACK_LOG, f"{now_shanghai()} {message}")


def record_monitor_state(monitors: dict[str, dict[str, Any]]) -> None:
    """保存组件快照，同时保留健康检查拥有的状态字段。"""
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
    write_json_atomic(HEARTBEAT_PATH, {"pid": pid, "checked_at": now_shanghai()})


def heartbeat_belongs_to(pid: int) -> bool:
    """退出清理前确认 heartbeat 仍属于当前 Supervisor 实例。"""
    return file_heartbeat_belongs_to(HEARTBEAT_PATH, pid)


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
        config["planner"]["scheduler"]["scheduled"] is True,
    )
    dispatcher = build(
        "dispatcher",
        "Dispatcher Scheduler",
        config["dispatcher"]["scheduled"] is True,
    )
    return planner, dispatcher


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
    value = read_json_object(path)
    if value is None:
        return None
    try:
        return {
            "component": str(value["component"]),
            "pid": int(value["pid"]),
            "status": str(value["status"]),
            "checked_at": str(value["checked_at"]),
        }
    except (KeyError, TypeError, ValueError):
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
    write_json_atomic(spec.stop_path, request)
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
        # 子进程已取得日志句柄，Supervisor 不长期占用父进程文件对象。
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
    runner_heartbeat_timeout_seconds: float = 120,
) -> dict[str, dict[str, Any]]:
    """管理三个常驻组件，并只读汇总动态 Runner 状态。"""
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
    try:
        runners = runner_snapshot(runner_heartbeat_timeout_seconds)
    except Exception as error:
        # 动态 Runner 的观察故障不能影响 Scheduler 管理或 Supervisor heartbeat。
        runners = {
            "component": "runners",
            "status": "UNAVAILABLE",
            "checked_at": now_shanghai(),
            "active_count": 0,
            "observed_count": 0,
            "message": f"Runner 状态读取失败：{type(error).__name__}",
            "instances": [],
        }
    return {
        "dashboard": dashboard,
        "planner": planner,
        "dispatcher": dispatcher,
        "runners": runners,
    }
