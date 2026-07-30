from __future__ import annotations

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
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop_previous_process() -> None:
    if not PID_PATH.exists():
        return
    try:
        pid = int(PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        PID_PATH.unlink(missing_ok=True)
        return
    if process_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(1.0)
        except OSError:
            pass
    PID_PATH.unlink(missing_ok=True)


def start_server(database_path: Path, config_path: Path) -> int:
    stop_previous_process()
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
            str(BASE_DIR / "scripts" / "dashboard_server.py"),
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
        if current and current.get("ok"):
            pid = int(PID_PATH.read_text(encoding="utf-8")) if PID_PATH.exists() else None
            record("HEALTHY", "Dashboard Server 正常。", 0, pid)
            output({"outcome": "HEALTHY", "url": url, "health": current, "pid": pid})

        state = read_state()
        failures = int(state.get("consecutive_failures", 0)) + 1
        if failures > threshold:
            record("NEEDS_ATTENTION", "Dashboard Server 连续恢复失败。", failures)
            output(
                {
                    "outcome": "NEEDS_ATTENTION",
                    "url": url,
                    "consecutive_failures": failures,
                    "threshold": threshold,
                },
                2,
            )
        pid = start_server(database_path, config_path)
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
