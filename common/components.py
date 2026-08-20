"""Supervisor 对 Scheduler 和 Dashboard 的组件管理方法。"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loopdb import now_shanghai

from common.files import (
    append_utf8_line,
    read_json_object,
    write_json_atomic,
)
from common.paths import (
    FALLBACK_LOG,
    HEALTH_STATE,
    REPOSITORY_ROOT,
)
from common.service_runtime import ServiceRuntimeFiles
from common.windows import process_alive, windows_powershell


@dataclass(frozen=True)
class ComponentSpec:
    """冻结一个独立服务的进程入口、参数、运行文件和监控阈值。"""

    key: str
    display_name: str
    enabled: bool
    entry: Path
    arguments: tuple[str, ...]
    runtime: ServiceRuntimeFiles
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


def record_monitor_state(
    monitors: dict[str, dict[str, Any]],
    *,
    supervisor_pid: int | None = None,
) -> None:
    """保存组件快照，并同步当前 Supervisor 的可展示状态。"""
    try:
        state = read_monitor_state()
        if supervisor_pid is not None:
            state.update(
                {
                    "component": "supervisor",
                    "status": "HEALTHY",
                    "pid": supervisor_pid,
                    "checked_at": now_shanghai(),
                    "consecutive_failures": 0,
                    "message": "Supervisor 主进程正常。",
                }
            )
        state["monitors"] = monitors
        state["monitor_checked_at"] = now_shanghai()
        write_monitor_state(state)
    except Exception as error:
        # 快照展示失败不能阻止 Supervisor 继续管理常驻组件。
        append_monitor_fallback(f"MONITOR_STATE_WRITE_FAILED {type(error).__name__}")


def component_specs(
    config: dict[str, Any],
    desired_states: dict[str, bool] | None = None,
) -> tuple[ComponentSpec, ...]:
    """从已校验配置构造 Dashboard、Scheduler 与 Runner 的运行契约。"""
    components = config["supervisor"]["components"]
    desired = desired_states or {}

    def build(
        key: str,
        display_name: str,
        enabled: bool,
        arguments: tuple[str, ...],
    ) -> ComponentSpec:
        raw = components[key]
        return ComponentSpec(
            key=key,
            display_name=display_name,
            enabled=enabled,
            entry=(REPOSITORY_ROOT / raw["entry"]).resolve(),
            arguments=arguments,
            runtime=ServiceRuntimeFiles.from_component_config(config, key),
            heartbeat_timeout_seconds=float(raw["heartbeat_timeout_seconds"]),
        )

    dashboard = build("dashboard", "Dashboard", True, ())
    scheduler = build(
        "scheduler",
        "Scheduler",
        (
            config["scheduler"]["preflight"]["scheduled"] is True
            or config["scheduler"]["execution"]["scheduled"] is True
        )
        and desired.get("scheduler", True),
        ("serve",),
    )
    runner = build(
        "runner",
        "Runner",
        desired.get("runner", True),
        ("serve",),
    )
    return dashboard, scheduler, runner


def is_component_process(pid: int, spec: ComponentSpec) -> bool:
    """核对 PID 存活，且命令行明确指向对应服务入口与固定参数。"""
    if not process_alive(pid):
        return False
    command_line = windows_powershell(
        f'(Get-CimInstance Win32_Process -Filter "ProcessId = {pid}").CommandLine'
    )
    if not command_line:
        return False
    has_entry = str(spec.entry).casefold() in command_line.casefold()
    has_arguments = all(
        re.search(rf"(?:^|\s){re.escape(argument)}(?:\s|$)", command_line, re.IGNORECASE)
        is not None
        for argument in spec.arguments
    )
    return has_entry and has_arguments


def component_health(
    spec: ComponentSpec,
    expected_pid: int | None = None,
) -> tuple[int | None, dict[str, Any] | None, str]:
    """联合 PID、heartbeat 和命令行身份判断组件是否可信。"""
    pid = spec.runtime.recorded_pid()
    if pid is None:
        return None, None, "PID 文件缺失或无效。"
    if expected_pid is not None and pid != expected_pid:
        return None, None, "PID 文件不属于刚启动的进程。"
    heartbeat = spec.runtime.read_heartbeat()
    heartbeat_problem = spec.runtime.heartbeat_problem(
        pid,
        spec.heartbeat_timeout_seconds,
    )
    if heartbeat_problem is not None:
        return None, heartbeat, heartbeat_problem
    if not is_component_process(pid, spec):
        return None, heartbeat, "PID 对应进程不存在或身份不匹配。"
    return pid, heartbeat, "组件正常运行。"


def remove_runtime_files(spec: ComponentSpec, pid: int | None) -> None:
    """只清理已停止实例拥有的运行文件，避免删除新实例状态。"""
    spec.runtime.clear(pid)


def request_component_stop(
    spec: ComponentSpec,
    pid: int,
    timeout_seconds: float,
    *,
    force_after_timeout: bool,
) -> bool:
    """向可信组件请求正常退出，恢复场景超时后才终止组件进程。"""
    if not process_alive(pid):
        remove_runtime_files(spec, pid)
        return True
    if not is_component_process(pid, spec):
        raise RuntimeError("PID 对应进程身份无法确认，拒绝发送停止请求。")
    spec.runtime.request_stop(pid)
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
    """以隐藏的独立后台进程启动组件，并把输出追加到其专用日志。"""
    spec.runtime.log_path.parent.mkdir(parents=True, exist_ok=True)
    spec.runtime.stop_path.unlink(missing_ok=True)
    log_stream = spec.runtime.log_path.open("a", encoding="utf-8", newline="\n")
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
                *spec.arguments,
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
    last_reason = "组件尚未写入 heartbeat。"
    while time.monotonic() < deadline:
        pid, heartbeat, last_reason = component_health(spec, expected_pid=process.pid)
        if pid is not None:
            return pid, heartbeat, last_reason
        if process.poll() is not None:
            return None, heartbeat, f"组件启动后退出，退出码 {process.returncode}。"
        time.sleep(0.2)
    return None, spec.runtime.read_heartbeat(), last_reason


def ensure_component(
    spec: ComponentSpec,
    database_path: Path,
    config_path: Path,
    start_timeout_seconds: float,
    stop_timeout_seconds: float,
) -> dict[str, Any]:
    """按配置开关确保组件已停止或处于健康运行状态。"""
    checked_at = now_shanghai()
    recorded = spec.runtime.recorded_pid()

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
                else f"{spec.display_name} 已收到停止请求；正在等待进程安全退出。"
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
    specs: tuple[ComponentSpec, ...],
    database_path: Path,
    config_path: Path,
    *,
    start_timeout_seconds: float,
    stop_timeout_seconds: float,
) -> dict[str, dict[str, Any]]:
    """管理 Dashboard、Scheduler 与 Runner 三个独立常驻组件。"""

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

    return {spec.key: inspect(spec) for spec in specs}
