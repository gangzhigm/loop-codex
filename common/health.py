"""Windows 计划任务使用的 Supervisor 探活与受控恢复方法。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from loopdb import json_dump, now_shanghai

from common.files import (
    append_utf8_line,
    read_json_object,
    read_pid,
    write_json_atomic,
)
from common.paths import (
    FALLBACK_LOG,
    HEALTH_LOCK,
    HEALTH_STATE,
    HEARTBEAT_PATH,
    PID_PATH,
    REPOSITORY_ROOT,
    SERVER_LOG,
)
from common.windows import listener_pids, process_alive, windows_powershell


def output(payload: dict[str, Any], exit_code: int = 0) -> None:
    """输出 UTF-8 JSON 结果并以指定退出码结束本次健康检查。"""
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(exit_code)


def append_fallback(message: str) -> None:
    """健康状态无法写入时，将最小诊断信息追加到后备日志。"""
    append_utf8_line(FALLBACK_LOG, f"{now_shanghai()} {message}")


def read_state() -> dict[str, Any]:
    """读取健康状态；文件缺失或损坏时返回安全初始值。"""
    return read_json_object(HEALTH_STATE) or {
        "consecutive_failures": 0,
        "last_checked_at": None,
        "events": [],
    }


def write_state(value: dict[str, Any]) -> None:
    """原子替换健康状态，避免中断留下不完整 JSON。"""
    write_json_atomic(HEALTH_STATE, value)


def acquire_lock() -> None:
    """建立健康检查互斥锁，并清理超过120秒的遗留锁。"""
    HEALTH_LOCK.parent.mkdir(parents=True, exist_ok=True)
    if HEALTH_LOCK.exists():
        age = time.time() - HEALTH_LOCK.stat().st_mtime
        if age < 120:
            output({"outcome": "BUSY", "message": "health supervisor already running"})
        HEALTH_LOCK.unlink()
    descriptor = os.open(HEALTH_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(json_dump({"pid": os.getpid(), "started_at": now_shanghai()}))


def release_lock() -> None:
    """幂等删除当前健康检查锁。"""
    HEALTH_LOCK.unlink(missing_ok=True)


def is_supervisor_process(pid: int) -> bool:
    """确认 PID 存活且命令行指向本项目 Supervisor serve 入口。"""
    if not process_alive(pid):
        return False
    command_line = windows_powershell(
        f'(Get-CimInstance Win32_Process -Filter "ProcessId = {pid}").CommandLine'
    )
    supervisor_entry = str(REPOSITORY_ROOT / "supervisor" / "main.py")
    return (
        supervisor_entry.casefold() in command_line.casefold()
        and "serve" in command_line.casefold()
    )


def recorded_pid() -> int | None:
    """读取 Supervisor PID 文件，内容不可信时返回 ``None``。"""
    return read_pid(PID_PATH)


def read_supervisor_heartbeat() -> dict[str, Any] | None:
    """读取主循环 heartbeat，并把关键字段转换为稳定类型。"""
    value = read_json_object(HEARTBEAT_PATH)
    if value is None:
        return None
    try:
        return {"pid": int(value["pid"]), "checked_at": str(value["checked_at"])}
    except (KeyError, TypeError, ValueError):
        return None


def supervisor_health(
    heartbeat_timeout_seconds: int,
    expected_pid: int | None = None,
) -> tuple[int | None, dict[str, Any] | None]:
    """核对 PID、进程身份及 heartbeat 新鲜度。"""
    pid = recorded_pid()
    if pid is None or (expected_pid is not None and pid != expected_pid):
        return None, None
    if not is_supervisor_process(pid):
        return None, None
    heartbeat = read_supervisor_heartbeat()
    if heartbeat is None or heartbeat["pid"] != pid:
        return None, heartbeat
    try:
        checked_at = datetime.fromisoformat(heartbeat["checked_at"])
        current = datetime.fromisoformat(now_shanghai())
        if checked_at.tzinfo is None or current.tzinfo is None:
            return None, heartbeat
    except (TypeError, ValueError):
        return None, heartbeat
    age = (current - checked_at).total_seconds()
    if age < 0 or age > heartbeat_timeout_seconds:
        return None, heartbeat
    return pid, heartbeat


def stop_previous_process(port: int) -> None:
    """停止本项目旧 Supervisor，并拒绝终止占用端口的外部进程。"""
    pid = recorded_pid()
    listeners = listener_pids(port)
    targets = {item for item in listeners if is_supervisor_process(item)}
    foreign_listeners = listeners - targets
    if foreign_listeners:
        raise RuntimeError(
            "Dashboard 端口被非本项目进程占用: "
            + ", ".join(str(item) for item in sorted(foreign_listeners))
        )
    if pid is not None and is_supervisor_process(pid):
        targets.add(pid)
    for target in targets:
        try:
            os.kill(target, signal.SIGTERM)
        except OSError:
            # 目标可能在身份检查后自行退出，其余目标仍需继续处理。
            pass
    deadline = time.monotonic() + 5.0
    while any(process_alive(target) for target in targets) and time.monotonic() < deadline:
        time.sleep(0.1)
    remaining = sorted(target for target in targets if process_alive(target))
    if remaining:
        raise RuntimeError(
            "Supervisor 主进程未在停止信号后退出: "
            + ", ".join(str(item) for item in remaining)
        )
    for _ in range(20):
        try:
            PID_PATH.unlink(missing_ok=True)
            HEARTBEAT_PATH.unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(0.1)
    PID_PATH.unlink(missing_ok=True)
    HEARTBEAT_PATH.unlink(missing_ok=True)


def start_server(database_path: Path, config_path: Path, port: int) -> int:
    """清理旧实例后，在后台启动 Supervisor 主进程并追加服务日志。"""
    stop_previous_process(port)
    log_stream = SERVER_LOG.open("a", encoding="utf-8", newline="\n")
    creation_flags = (
        subprocess.CREATE_NEW_PROCESS_GROUP
        | subprocess.DETACHED_PROCESS
        | subprocess.CREATE_NO_WINDOW
    )
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(REPOSITORY_ROOT / "supervisor" / "main.py"),
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
            close_fds=True,
            creationflags=creation_flags,
        )
    finally:
        # 子进程已取得日志句柄，健康检查不长期占用父进程文件对象。
        log_stream.close()
    return process.pid


def record(status: str, message: str, failures: int, pid: int | None = None) -> None:
    """更新健康快照并只保留最近100条事件。"""
    try:
        state = read_state()
        monitors = state.get("monitors")
        checked_at = now_shanghai()
        events = list(state.get("events") or [])
        events.insert(
            0,
            {
                "at": checked_at,
                "component": "supervisor",
                "status": status,
                "message": message,
                "details": {"pid": pid, "failures": failures},
            },
        )
        value = {
            "component": "supervisor",
            "status": status,
            "pid": pid,
            "checked_at": checked_at,
            "last_checked_at": checked_at,
            "consecutive_failures": failures,
            "message": message,
            "events": events[:100],
        }
        if isinstance(monitors, dict):
            value["monitors"] = monitors
        write_state(value)
    except Exception as error:
        append_fallback(f"{status} {message}; runtime state write failed: {error}")
