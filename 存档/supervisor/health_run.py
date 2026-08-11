"""Supervisor 的单次健康检查与受控恢复程序。

脚本由计划任务周期调用，不常驻轮询。每次运行先取得文件锁，探测 ``/healthz`` 并
核对端口监听者、PID 文件与实际进程身份；只有健康或拓扑异常时才尝试清理旧实例并
启动新的 Supervisor 主进程。检查结果写入有限长度的健康状态文件，写入失败则退化到文本日志。

本程序只管理 Supervisor 主进程（当前由其承载 Dashboard），不领取任务、不回收 Worker execution，也不会把
“进程消失”解释为任务已经安全结束。
"""

from __future__ import annotations

# 中文排查：健康任务负责探活 Supervisor、维护 PID/健康 JSON，并在阈值满足时恢复主进程。
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

SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"
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
    """输出 UTF-8 JSON 结果并以指定退出码立即结束本次健康检查。"""
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(exit_code)


def append_fallback(message: str) -> None:
    """健康状态 JSON 无法写入时，将最小诊断信息追加到后备日志。"""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with FALLBACK_LOG.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(f"{now_shanghai()} {message}\n")


def read_state() -> dict[str, Any]:
    """读取上次健康状态；文件缺失、损坏或不可读时返回安全初始值。"""
    if not HEALTH_STATE.exists():
        return {"consecutive_failures": 0, "last_checked_at": None, "events": []}
    try:
        return json.loads(HEALTH_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"consecutive_failures": 0, "last_checked_at": None, "events": []}


def write_state(value: dict[str, Any]) -> None:
    """先写临时文件再原子替换健康状态，避免中断留下半截 JSON。"""
    temporary = HEALTH_STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(HEALTH_STATE)


def acquire_lock() -> None:
    """建立健康检查互斥锁，并清理超过 120 秒的遗留锁。

    新锁使用 ``O_EXCL`` 创建，确保两个计划任务实例不能同时重启 Dashboard。
    """
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
    """幂等删除当前健康检查锁；锁已不存在时不报错。"""
    try:
        HEALTH_LOCK.unlink()
    except FileNotFoundError:
        pass


def health_request(url: str, timeout: float = 3.0) -> dict[str, Any] | None:
    """请求 healthz 并解析 JSON；网络、系统或格式错误统一返回 ``None``。"""
    try:
        with urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (URLError, OSError, json.JSONDecodeError):
        return None


def process_alive(pid: int) -> bool:
    """跨平台判断 PID 是否仍活动；Windows 使用进程退出码避免发送信号。"""
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
    """隐藏运行 PowerShell 查询命令，失败时返回空字符串供身份校验拒绝通过。"""
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
    """在 Windows 上从 netstat 提取指定 TCP 端口的全部监听 PID。"""
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
    """确认 PID 存活且 Windows 命令行确实指向本项目 Supervisor 主入口。

    Dashboard 仍使用既有 PID 文件记录常驻主进程，因此此函数名称暂不改变，避免影响
    同文件内的端口拓扑逻辑。新恢复实例必须是 ``supervisor/main.py serve``；旧的直接
    Dashboard 进程不会再被视为可复用实例，会由本次健康检查安全替换。
    """
    if not process_alive(pid):
        return False
    if os.name != "nt":
        return True
    command_line = windows_powershell(
        f'(Get-CimInstance Win32_Process -Filter "ProcessId = {pid}").CommandLine'
    )
    supervisor_entry = str(BASE_DIR / "supervisor" / "main.py")
    return (
        supervisor_entry.casefold() in command_line.casefold()
        and "serve" in command_line.casefold()
    )


def is_recoverable_dashboard_process(pid: int) -> bool:
    """确认端口进程属于本项目的当前或旧版 Dashboard 实现。

    健康判断只接受新的 Supervisor 主入口；但恢复前需要允许停止旧版直接启动的
    ``dashboard_server.py``，否则升级后的首轮检查会把它误判为外部程序并拒绝恢复。
    这里绝不接受没有明确脚本路径的 Python 进程。
    """
    if is_dashboard_process(pid):
        return True
    if not process_alive(pid) or os.name != "nt":
        return False
    command_line = windows_powershell(
        f'(Get-CimInstance Win32_Process -Filter "ProcessId = {pid}").CommandLine'
    )
    legacy_entry = str(
        BASE_DIR / "scripts" / "roles" / "supervisor" / "dashboard_server.py"
    )
    return legacy_entry.casefold() in command_line.casefold()


def recorded_pid() -> int | None:
    """读取 PID 文件；缺失、不可读或非整数内容都视为没有可信记录。"""
    if not PID_PATH.exists():
        return None
    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def dashboard_topology_is_clean(port: int) -> bool:
    """确认端口只有一个监听者，且它与 PID 文件及 Dashboard 身份一致。"""
    if os.name != "nt":
        return True
    listeners = listener_pids(port)
    pid = recorded_pid()
    return len(listeners) == 1 and pid in listeners and all(
        is_dashboard_process(item) for item in listeners
    )


def stop_previous_process(port: int) -> None:
    """停止本项目占用端口的旧 Dashboard，并拒绝终止外部进程。

    函数先区分 Dashboard 与非本项目监听者；发现外部监听者立即报错。合法目标收到
    终止信号后最多等待五秒，仍未退出则中止恢复，避免强杀未知状态进程。
    """
    pid = recorded_pid()
    listeners = listener_pids(port)
    targets = {item for item in listeners if is_recoverable_dashboard_process(item)}
    foreign_listeners = listeners - targets
    if foreign_listeners:
        raise RuntimeError(
            "Dashboard 端口被非本项目进程占用: "
            + ", ".join(str(item) for item in sorted(foreign_listeners))
        )
    if pid is not None and is_recoverable_dashboard_process(pid):
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
    """清理旧实例后在后台启动 Supervisor 主进程，并追加服务日志。"""
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
            str(BASE_DIR / "supervisor" / "main.py"),
            "serve",
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
    """更新健康快照并只保留最近 100 条事件；失败时写入后备日志。"""
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


def main(argv: list[str] | None = None) -> None:
    """执行一次探活、必要恢复、重试验证和阈值告警流程。

    健康且拓扑干净时直接返回。否则连续失败数加一并尝试启动；恢复成功会清零失败数，
    恢复失败则按配置阈值返回 ``UNHEALTHY`` 或 ``NEEDS_ATTENTION``。
    """
    parser = argparse.ArgumentParser(description="Local Agent Loop Supervisor health check")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args(argv)
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
            record("HEALTHY", "Supervisor 主进程正常。", 0, pid)
            output({"outcome": "HEALTHY", "url": url, "health": current, "pid": pid})

        state = read_state()
        failures = int(state.get("consecutive_failures", 0)) + 1
        try:
            pid = start_server(database_path, config_path, port)
        except (OSError, RuntimeError) as error:
            status = "NEEDS_ATTENTION" if failures >= threshold else "UNHEALTHY"
            message = f"Supervisor 主进程恢复启动失败：{error}"
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
            record("RESTARTED", "Supervisor 主进程已启动或恢复。", 0, pid)
            output({"outcome": "RESTARTED", "url": url, "pid": pid, "health": recovered})
        status = "NEEDS_ATTENTION" if failures >= threshold else "UNHEALTHY"
        message = (
            "Supervisor 主进程连续恢复失败，已达到告警阈值。"
            if status == "NEEDS_ATTENTION"
            else "Supervisor 主进程启动后仍不可用。"
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
