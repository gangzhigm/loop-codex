"""Codex CLI Dispatcher 的单实例常驻调度入口。

本进程维护自己的 PID 与 heartbeat，并按配置周期执行一次现有 Dispatcher 调度。
Supervisor 只管理本进程，不读取任务数据库，也不直接启动 Runner。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = REPOSITORY_ROOT / "control"
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))

from loopdb import CONFIG_PATH, DEFAULT_DB, load_initialization_config, now_shanghai
from dispatcher.codex_cli_dispatcher import CodexCliDispatcher, DispatcherSettings


def runtime_path(config: dict[str, Any], key: str) -> Path:
    """从 Supervisor 的 Dispatcher 运行契约解析项目内路径。"""
    value = config["supervisor"]["components"]["dispatcher"][key]
    return (REPOSITORY_ROOT / str(value)).resolve()


def write_heartbeat(path: Path, pid: int) -> None:
    """原子写入 Dispatcher Scheduler 的当前进程身份和推进时间。"""
    value = {
        "component": "dispatcher",
        "pid": pid,
        "status": "RUNNING",
        "checked_at": now_shanghai(),
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def heartbeat_belongs_to(path: Path, pid: int) -> bool:
    """确认退出时看到的 heartbeat 仍属于当前 Dispatcher Scheduler。"""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(value, dict) and int(value["pid"]) == pid
    except (FileNotFoundError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False


def parser() -> argparse.ArgumentParser:
    """创建 Dispatcher Scheduler 常驻命令行。"""
    value = argparse.ArgumentParser(description="Local Agent Loop Dispatcher Scheduler")
    commands = value.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="常驻运行 Dispatcher Scheduler")
    serve.add_argument("--db", default=str(DEFAULT_DB))
    serve.add_argument("--config", default=str(CONFIG_PATH))
    return value


def serve_dispatcher(args: argparse.Namespace) -> None:
    """保持单实例 Dispatcher 存活，并按配置周期执行现有单轮调度。"""
    config_path = Path(args.config).resolve()
    database_path = Path(args.db).resolve()
    config = load_initialization_config(config_path)
    settings = DispatcherSettings.from_config(config, config_path=config_path)
    if settings.scheduled is not True:
        raise SystemExit("Dispatcher 自动调度已关闭")
    if database_path != settings.database_path:
        settings = DispatcherSettings(**{**settings.__dict__, "database_path": database_path})

    heartbeat_interval = float(config["codex_cli"]["dispatcher"]["heartbeat_interval_seconds"])
    dispatch_interval = float(settings.interval_minutes) * 60
    pid_path = runtime_path(config, "pid_path")
    heartbeat_path = runtime_path(config, "heartbeat_path")
    stop_path = runtime_path(config, "stop_path")
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    # 上一个实例已退出后遗留的停止请求不能阻止新实例启动。
    stop_path.unlink(missing_ok=True)
    pid = os.getpid()
    shutdown_event = threading.Event()

    try:
        descriptor = os.open(pid_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise SystemExit("Dispatcher Scheduler PID 文件已存在") from None
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(str(pid))

    def stop(signum: int, frame: object) -> None:
        """将系统终止信号转换为主循环可观察的停止事件。"""
        del signum, frame
        shutdown_event.set()

    try:
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        print(f"{now_shanghai()} dispatcher scheduler started", flush=True)
        next_heartbeat = time.monotonic()
        next_dispatch = time.monotonic()
        while not shutdown_event.is_set():
            # 停止 Scheduler 只会阻止后续分发，已经启动的 Runner 独立完成自己的任务。
            if stop_path.exists():
                break
            current = time.monotonic()
            if current >= next_heartbeat:
                write_heartbeat(heartbeat_path, pid)
                next_heartbeat = current + heartbeat_interval
            if current >= next_dispatch:
                result = CodexCliDispatcher(settings, config).run()
                print(json.dumps(result, ensure_ascii=False), flush=True)
                next_dispatch = time.monotonic() + dispatch_interval
            current = time.monotonic()
            wait_seconds = min(
                1.0,
                max(0.0, next_heartbeat - current),
                max(0.0, next_dispatch - current),
            )
            shutdown_event.wait(wait_seconds)
    finally:
        shutdown_event.set()
        if pid_path.exists() and pid_path.read_text(encoding="utf-8").strip() == str(pid):
            pid_path.unlink()
        if heartbeat_belongs_to(heartbeat_path, pid):
            heartbeat_path.unlink(missing_ok=True)
        stop_path.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> None:
    """解析 serve 参数并启动 Dispatcher Scheduler。"""
    args = parser().parse_args(argv)
    serve_dispatcher(args)


if __name__ == "__main__":
    main()
