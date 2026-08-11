from __future__ import annotations

# 中文排查：健康任务负责探活 Dashboard、维护 PID/健康 JSON，并在阈值满足时恢复服务。
# 异常依次检查互斥锁、PID 归属、healthz 响应、数据库校验和新进程启动日志。
# 它不领取 AI 任务，也不能根据健康信号推断任何 Worker 会话已经安全结束。

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

sys.dont_write_bytecode = True

SCRIPTS_ROOT = Path(__file__).resolve().parents[2]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from loopdb import (
    BASE_DIR,
    CONFIG_PATH,
    DEFAULT_DB,
    json_dump,
    load_initialization_config,
    now_shanghai,
)


RUNTIME_DIR = BASE_DIR / "runtime"
HEALTH_LOCK = RUNTIME_DIR / "health-supervisor.lock"
HEALTH_STATE = RUNTIME_DIR / "health-state.json"
PID_PATH = RUNTIME_DIR / "dashboard-server.pid"
FALLBACK_LOG = RUNTIME_DIR / "health-fallback.log"
SERVER_LOG = RUNTIME_DIR / "dashboard-server.log"


def output(payload: dict[str, Any], exit_code: int = 0) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(exit_code)


def append_fallback(message: str) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with FALLBACK_LOG.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(f"{now_shanghai()} {message}\n")


def read_state() -> dict[str, Any]:
    if not HEALTH_STATE.exists():
        return {"consecutive_failures": 0, "last_checked_at": None, "events": []}
    try:
        return json.loads(HEALTH_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"consecutive_failures": 0, "last_checked_at": None, "events": []}


def write_state(value: dict[str, Any]) -> None:
    temporary = HEALTH_STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(HEALTH_STATE)


def acquire_lock() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    if HEALTH_LOCK.exists():
        age = time.time() - HEALTH_LOCK.stat().st_mtime
        if age < 120:
            output({"outcome": "BUSY", "message": "health supervisor already running"})
        HEALTH_LOCK.unlink()
    descriptor = os.open(HEALTH_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(json_dump({"pid": os.getpid(), "started_at": now_shanghai()}))


def release_lock() -> None:
    try:
        HEALTH_LOCK.unlink()
    except FileNotFoundError:
        pass


def health_request(url: str, timeout: float = 3.0) -> dict[str, Any] | None:
    try:
        with urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (URLError, OSError, json.JSONDecodeError):
        return None


def process_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

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
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def windows_powershell(command: str) -> str:
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def listener_pids(port: int) -> set[int]:
    if os.name != "nt":
        return set()
    completed = subprocess.run(
        ["netstat.exe", "-ano", "-p", "tcp"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    result: set[int] = set()
    if completed.returncode != 0:
        return result
    for line in completed.stdout.splitlines():
        columns = line.split()
        if len(columns) < 5 or columns[0].upper() != "TCP" or columns[3].upper() != "LISTENING":
            continue
        if columns[1].rsplit(":", 1)[-1] != str(port):
            continue
        try:
            result.add(int(columns[4]))
        except ValueError:
            continue
    return result


def is_dashboard_process(pid: int) -> bool:
    if not process_alive(pid):
        return False
    if os.name != "nt":
        return True
    command_line = windows_powershell(
        f'(Get-CimInstance Win32_Process -Filter "ProcessId = {pid}").CommandLine'
    )
    dashboard_script = str(
        BASE_DIR / "scripts" / "roles" / "supervisor" / "dashboard_server.py"
    )
    return dashboard_script.casefold() in command_line.casefold()


def recorded_pid() -> int | None:
    if not PID_PATH.exists():
        return None
    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def dashboard_topology_is_clean(port: int) -> bool:
    if os.name != "nt":
        return True
    listeners = listener_pids(port)
    pid = recorded_pid()
    return len(listeners) == 1 and pid in listeners and all(
        is_dashboard_process(item) for item in listeners
    )


def stop_previous_process(port: int) -> None:
    pid = recorded_pid()
    listeners = listener_pids(port)
    targets = {item for item in listeners if is_dashboard_process(item)}
    foreign_listeners = listeners - targets
    if foreign_listeners:
        raise RuntimeError(
            "Dashboard 端口被非本项目进程占用: "
            + ", ".join(str(item) for item in sorted(foreign_listeners))
        )
    if pid is not None and is_dashboard_process(pid):
        targets.add(pid)
    for target in targets:
        try:
            os.kill(target, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + 5.0
    while any(process_alive(target) for target in targets) and time.monotonic() < deadline:
        time.sleep(0.1)
    remaining = sorted(target for target in targets if process_alive(target))
    if remaining:
        raise RuntimeError(
            "Dashboard Server 进程未在停止信号后退出: "
            + ", ".join(str(item) for item in remaining)
        )
    for _ in range(20):
        try:
            PID_PATH.unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(0.1)
    PID_PATH.unlink(missing_ok=True)


def start_server(database_path: Path, config_path: Path, port: int) -> int:
    stop_previous_process(port)
    log_stream = SERVER_LOG.open("a", encoding="utf-8", newline="\n")
    creation_flags = 0
    if os.name == "nt":
        creation_flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
    process = subprocess.Popen(
        [
            sys.executable,
            "-B",
            str(
                BASE_DIR
                / "scripts"
                / "roles"
                / "supervisor"
                / "dashboard_server.py"
            ),
            "--db",
            str(database_path),
            "--config",
            str(config_path),
        ],
        cwd=str(BASE_DIR),
        stdin=subprocess.DEVNULL,
        stdout=log_stream,
        stderr=log_stream,
        close_fds=True,
        creationflags=creation_flags,
    )
    log_stream.close()
    return process.pid


def record(status: str, message: str, failures: int, pid: int | None = None) -> None:
    try:
        state = read_state()
        checked_at = now_shanghai()
        events = list(state.get("events") or [])
        events.insert(
            0,
            {
                "at": checked_at,
                "component": "dashboard-server",
                "status": status,
                "message": message,
                "details": {"pid": pid, "failures": failures},
            },
        )
        write_state(
            {
                "component": "dashboard-server",
                "status": status,
                "pid": pid,
                "checked_at": checked_at,
                "last_checked_at": checked_at,
                "consecutive_failures": failures,
                "message": message,
                "events": events[:100],
            }
        )
    except Exception as error:
        append_fallback(f"{status} {message}; runtime state write failed: {error}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Agent Loop dashboard health supervisor")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args()
    database_path = Path(args.db).resolve()
    config_path = Path(args.config).resolve()
    acquire_lock()
    try:
        config = load_initialization_config(config_path)
        host = str(config["dashboard"]["host"])
        port = int(config["dashboard"]["port"])
        threshold = int(config["health"]["failure_threshold"])
        url = f"http://{host}:{port}/healthz"
        current = health_request(url)
        if current and current.get("ok") and dashboard_topology_is_clean(port):
            pid = int(PID_PATH.read_text(encoding="utf-8")) if PID_PATH.exists() else None
            record("HEALTHY", "Dashboard Server 正常。", 0, pid)
            output({"outcome": "HEALTHY", "url": url, "health": current, "pid": pid})

        state = read_state()
        failures = int(state.get("consecutive_failures", 0)) + 1
        try:
            pid = start_server(database_path, config_path, port)
        except (OSError, RuntimeError) as error:
            status = "NEEDS_ATTENTION" if failures >= threshold else "UNHEALTHY"
            message = f"Dashboard Server 恢复启动失败：{error}"
            record(status, message, failures)
            output(
                {
                    "outcome": status,
                    "url": url,
                    "message": message,
                    "consecutive_failures": failures,
                    "threshold": threshold,
                },
                2 if status == "NEEDS_ATTENTION" else 1,
            )
        recovered = None
        for _ in range(20):
            time.sleep(0.5)
            recovered = health_request(url, timeout=1.0)
            if recovered and recovered.get("ok"):
                break
        if recovered and recovered.get("ok"):
            record("RESTARTED", "Dashboard Server 已启动或恢复。", 0, pid)
            output({"outcome": "RESTARTED", "url": url, "pid": pid, "health": recovered})
        status = "NEEDS_ATTENTION" if failures >= threshold else "UNHEALTHY"
        message = (
            "Dashboard Server 连续恢复失败，已达到告警阈值。"
            if status == "NEEDS_ATTENTION"
            else "Dashboard Server 启动后仍不可用。"
        )
        record(status, message, failures, pid)
        output(
            {
                "outcome": status,
                "url": url,
                "pid": pid,
                "consecutive_failures": failures,
                "threshold": threshold,
            },
            2 if status == "NEEDS_ATTENTION" else 1,
        )
    finally:
        release_lock()


if __name__ == "__main__":
    main()
