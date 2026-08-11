"""Supervisor 的单次健康检查与受控恢复程序。

脚本由计划任务周期调用，不常驻轮询。每次运行先取得文件锁，探测 ``/healthz`` 并
核对端口监听者、PID 文件与实际进程身份；只有健康或拓扑异常时才尝试清理旧实例并
启动新的 Supervisor 主进程。检查结果写入有限长度的健康状态文件，写入失败则退化到文本日志。

本程序只管理 Supervisor 主进程（其承载 Dashboard 并运行组件监控），不领取任务、不回收 Worker execution，
也不会把“进程消失”解释为任务已经安全结束。
"""

# 说明下一条语句的作用。
from __future__ import annotations

# 中文排查：健康任务负责探活 Supervisor、维护 PID/健康 JSON，并在阈值满足时恢复主进程。
# 异常依次检查互斥锁、PID 归属、healthz 响应、数据库校验和新进程启动日志。
# 它不领取 AI 任务，也不能根据健康信号推断任何 Worker 会话已经安全结束。

# 说明下一条语句的作用。
import argparse
# 说明下一条语句的作用。
import json
# 说明下一条语句的作用。
import os
# 说明下一条语句的作用。
import signal
# 说明下一条语句的作用。
import subprocess
# 说明下一条语句的作用。
import sys
# 说明下一条语句的作用。
import time
# 说明下一条语句的作用。
from pathlib import Path
# 说明下一条语句的作用。
from typing import Any
# 说明下一条语句的作用。
from urllib.error import URLError
# 说明下一条语句的作用。
from urllib.request import urlopen

# 说明下一条语句的作用。
sys.dont_write_bytecode = True

# 说明下一条语句的作用。
SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"
# 说明下一条语句的作用。
if str(SCRIPTS_ROOT) not in sys.path:
    # 说明下一条语句的作用。
    sys.path.insert(0, str(SCRIPTS_ROOT))

# 说明下一条语句的作用。
from loopdb import (
    # 说明下一条语句的作用。
    BASE_DIR,
    # 说明下一条语句的作用。
    CONFIG_PATH,
    # 说明下一条语句的作用。
    DEFAULT_DB,
    # 说明下一条语句的作用。
    json_dump,
    # 说明下一条语句的作用。
    load_initialization_config,
    # 说明下一条语句的作用。
    now_shanghai,
# 说明下一条语句的作用。
)


# 说明下一条语句的作用。
RUNTIME_DIR = BASE_DIR / "runtime"
# 说明下一条语句的作用。
HEALTH_LOCK = RUNTIME_DIR / "health-supervisor.lock"
# 说明下一条语句的作用。
HEALTH_STATE = RUNTIME_DIR / "health-state.json"
# 说明下一条语句的作用。
PID_PATH = RUNTIME_DIR / "dashboard-server.pid"
# 说明下一条语句的作用。
FALLBACK_LOG = RUNTIME_DIR / "health-fallback.log"
# 说明下一条语句的作用。
SERVER_LOG = RUNTIME_DIR / "dashboard-server.log"


# 说明下一条语句的作用。
def output(payload: dict[str, Any], exit_code: int = 0) -> None:
    """输出 UTF-8 JSON 结果并以指定退出码立即结束本次健康检查。"""
    # 说明下一条语句的作用。
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    # 说明下一条语句的作用。
    raise SystemExit(exit_code)


# 说明下一条语句的作用。
def append_fallback(message: str) -> None:
    """健康状态 JSON 无法写入时，将最小诊断信息追加到后备日志。"""
    # 说明下一条语句的作用。
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    # 说明下一条语句的作用。
    with FALLBACK_LOG.open("a", encoding="utf-8", newline="\n") as stream:
        # 说明下一条语句的作用。
        stream.write(f"{now_shanghai()} {message}\n")


# 说明下一条语句的作用。
def read_state() -> dict[str, Any]:
    """读取上次健康状态；文件缺失、损坏或不可读时返回安全初始值。"""
    # 说明下一条语句的作用。
    if not HEALTH_STATE.exists():
        # 说明下一条语句的作用。
        return {"consecutive_failures": 0, "last_checked_at": None, "events": []}
    # 说明下一条语句的作用。
    try:
        # 说明下一条语句的作用。
        return json.loads(HEALTH_STATE.read_text(encoding="utf-8"))
    # 说明下一条语句的作用。
    except (OSError, json.JSONDecodeError):
        # 说明下一条语句的作用。
        return {"consecutive_failures": 0, "last_checked_at": None, "events": []}


# 说明下一条语句的作用。
def write_state(value: dict[str, Any]) -> None:
    """先写临时文件再原子替换健康状态，避免中断留下半截 JSON。"""
    # 说明下一条语句的作用。
    temporary = HEALTH_STATE.with_suffix(".tmp")
    # 说明下一条语句的作用。
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # 说明下一条语句的作用。
    temporary.replace(HEALTH_STATE)


# 说明下一条语句的作用。
def record_monitor_state(monitors: dict[str, dict[str, Any]]) -> None:
    """保存常驻 Supervisor 的组件快照，并保留最近一次外部健康检查结果。

    ``health`` 与 ``serve`` 都可能更新同一个健康文件。两者只保留自己拥有的字段：
    这里更新 ``monitors``，健康检查的 ``record`` 会保留该字段，避免把组件状态误当作
    任务状态写入 SQLite。
    """
    # 说明下一条语句的作用。
    try:
        # 说明下一条语句的作用。
        state = read_state()
        # 说明下一条语句的作用。
        state["monitors"] = monitors
        # 说明下一条语句的作用。
        state["monitor_checked_at"] = now_shanghai()
        # 说明下一条语句的作用。
        write_state(state)
    # 说明下一条语句的作用。
    except Exception as error:
        # 说明下一条语句的作用。
        append_fallback(f"MONITOR_STATE_WRITE_FAILED {type(error).__name__}")


# 说明下一条语句的作用。
def acquire_lock() -> None:
    """建立健康检查互斥锁，并清理超过 120 秒的遗留锁。

    新锁使用 ``O_EXCL`` 创建，确保两个计划任务实例不能同时重启 Dashboard。
    """
    # 说明下一条语句的作用。
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    # 说明下一条语句的作用。
    if HEALTH_LOCK.exists():
        # 说明下一条语句的作用。
        age = time.time() - HEALTH_LOCK.stat().st_mtime
        # 说明下一条语句的作用。
        if age < 120:
            # 说明下一条语句的作用。
            output({"outcome": "BUSY", "message": "health supervisor already running"})
        # 说明下一条语句的作用。
        HEALTH_LOCK.unlink()
    # 说明下一条语句的作用。
    descriptor = os.open(HEALTH_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    # 说明下一条语句的作用。
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        # 说明下一条语句的作用。
        stream.write(json_dump({"pid": os.getpid(), "started_at": now_shanghai()}))


# 说明下一条语句的作用。
def release_lock() -> None:
    """幂等删除当前健康检查锁；锁已不存在时不报错。"""
    # 说明下一条语句的作用。
    try:
        # 说明下一条语句的作用。
        HEALTH_LOCK.unlink()
    # 说明下一条语句的作用。
    except FileNotFoundError:
        # 说明下一条语句的作用。
        pass


# 说明下一条语句的作用。
def health_request(url: str, timeout: float = 3.0) -> dict[str, Any] | None:
    """请求 healthz 并解析 JSON；网络、系统或格式错误统一返回 ``None``。"""
    # 说明下一条语句的作用。
    try:
        # 说明下一条语句的作用。
        with urlopen(url, timeout=timeout) as response:
            # 说明下一条语句的作用。
            return json.loads(response.read().decode("utf-8"))
    # 说明下一条语句的作用。
    except (URLError, OSError, json.JSONDecodeError):
        # 说明下一条语句的作用。
        return None


# 说明下一条语句的作用。
def process_alive(pid: int) -> bool:
    """跨平台判断 PID 是否仍活动；Windows 使用进程退出码避免发送信号。"""
    # 说明下一条语句的作用。
    if os.name == "nt":
        # 说明下一条语句的作用。
        import ctypes

        # 说明下一条语句的作用。
        process_query_limited_information = 0x1000
        # 说明下一条语句的作用。
        still_active = 259
        # 说明下一条语句的作用。
        kernel32 = ctypes.windll.kernel32
        # 说明下一条语句的作用。
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        # 说明下一条语句的作用。
        if not handle:
            # 说明下一条语句的作用。
            return False
        # 说明下一条语句的作用。
        try:
            # 说明下一条语句的作用。
            exit_code = ctypes.c_ulong()
            # 说明下一条语句的作用。
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                # 说明下一条语句的作用。
                return False
            # 说明下一条语句的作用。
            return exit_code.value == still_active
        # 说明下一条语句的作用。
        finally:
            # 说明下一条语句的作用。
            kernel32.CloseHandle(handle)
    # 说明下一条语句的作用。
    try:
        # 说明下一条语句的作用。
        os.kill(pid, 0)
        # 说明下一条语句的作用。
        return True
    # 说明下一条语句的作用。
    except OSError:
        # 说明下一条语句的作用。
        return False


# 说明下一条语句的作用。
def windows_powershell(command: str) -> str:
    """隐藏运行 PowerShell 查询命令，失败时返回空字符串供身份校验拒绝通过。"""
    # 说明下一条语句的作用。
    completed = subprocess.run(
        # 说明下一条语句的作用。
        ["powershell.exe", "-NoProfile", "-Command", command],
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
    )
    # 说明下一条语句的作用。
    if completed.returncode != 0:
        # 说明下一条语句的作用。
        return ""
    # 说明下一条语句的作用。
    return completed.stdout.strip()


# 说明下一条语句的作用。
def listener_pids(port: int) -> set[int]:
    """在 Windows 上从 netstat 提取指定 TCP 端口的全部监听 PID。"""
    # 说明下一条语句的作用。
    if os.name != "nt":
        # 说明下一条语句的作用。
        return set()
    # 说明下一条语句的作用。
    completed = subprocess.run(
        # 说明下一条语句的作用。
        ["netstat.exe", "-ano", "-p", "tcp"],
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
    )
    # 说明下一条语句的作用。
    result: set[int] = set()
    # 说明下一条语句的作用。
    if completed.returncode != 0:
        # 说明下一条语句的作用。
        return result
    # 说明下一条语句的作用。
    for line in completed.stdout.splitlines():
        # 说明下一条语句的作用。
        columns = line.split()
        # 说明下一条语句的作用。
        if len(columns) < 5 or columns[0].upper() != "TCP" or columns[3].upper() != "LISTENING":
            # 说明下一条语句的作用。
            continue
        # 说明下一条语句的作用。
        if columns[1].rsplit(":", 1)[-1] != str(port):
            # 说明下一条语句的作用。
            continue
        # 说明下一条语句的作用。
        try:
            # 说明下一条语句的作用。
            result.add(int(columns[4]))
        # 说明下一条语句的作用。
        except ValueError:
            # 说明下一条语句的作用。
            continue
    # 说明下一条语句的作用。
    return result


# 说明下一条语句的作用。
def is_dashboard_process(pid: int) -> bool:
    """确认 PID 存活且 Windows 命令行确实指向本项目 Supervisor 主入口。

    Dashboard 仍使用既有 PID 文件记录常驻主进程，因此此函数名称暂不改变，避免影响
    同文件内的端口拓扑逻辑。新恢复实例必须是 ``supervisor/main.py serve``；旧的直接
    Dashboard 进程不会再被视为可复用实例，会由本次健康检查安全替换。
    """
    # 说明下一条语句的作用。
    if not process_alive(pid):
        # 说明下一条语句的作用。
        return False
    # 说明下一条语句的作用。
    if os.name != "nt":
        # 说明下一条语句的作用。
        return True
    # 说明下一条语句的作用。
    command_line = windows_powershell(
        # 说明下一条语句的作用。
        f'(Get-CimInstance Win32_Process -Filter "ProcessId = {pid}").CommandLine'
    # 说明下一条语句的作用。
    )
    # 说明下一条语句的作用。
    supervisor_entry = str(BASE_DIR / "supervisor" / "main.py")
    # 说明下一条语句的作用。
    return (
        # 说明下一条语句的作用。
        supervisor_entry.casefold() in command_line.casefold()
        # 说明下一条语句的作用。
        and "serve" in command_line.casefold()
    # 说明下一条语句的作用。
    )


# 说明下一条语句的作用。
def is_recoverable_dashboard_process(pid: int) -> bool:
    """确认端口进程属于本项目的当前或旧版 Dashboard 实现。

    健康判断只接受新的 Supervisor 主入口；但恢复前需要允许停止旧版直接启动的
    ``dashboard_server.py``，否则升级后的首轮检查会把它误判为外部程序并拒绝恢复。
    这里绝不接受没有明确脚本路径的 Python 进程。
    """
    # 说明下一条语句的作用。
    if is_dashboard_process(pid):
        # 说明下一条语句的作用。
        return True
    # 说明下一条语句的作用。
    if not process_alive(pid) or os.name != "nt":
        # 说明下一条语句的作用。
        return False
    # 说明下一条语句的作用。
    command_line = windows_powershell(
        # 说明下一条语句的作用。
        f'(Get-CimInstance Win32_Process -Filter "ProcessId = {pid}").CommandLine'
    # 说明下一条语句的作用。
    )
    # 说明下一条语句的作用。
    legacy_entry = str(
        # 说明下一条语句的作用。
        BASE_DIR / "scripts" / "roles" / "supervisor" / "dashboard_server.py"
    # 说明下一条语句的作用。
    )
    # 说明下一条语句的作用。
    return legacy_entry.casefold() in command_line.casefold()


# 说明下一条语句的作用。
def recorded_pid() -> int | None:
    """读取 PID 文件；缺失、不可读或非整数内容都视为没有可信记录。"""
    # 说明下一条语句的作用。
    if not PID_PATH.exists():
        # 说明下一条语句的作用。
        return None
    # 说明下一条语句的作用。
    try:
        # 说明下一条语句的作用。
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    # 说明下一条语句的作用。
    except (OSError, ValueError):
        # 说明下一条语句的作用。
        return None


# 说明下一条语句的作用。
def dashboard_topology_is_clean(port: int) -> bool:
    """确认端口只有一个监听者，且它与 PID 文件及 Dashboard 身份一致。"""
    # 说明下一条语句的作用。
    if os.name != "nt":
        # 说明下一条语句的作用。
        return True
    # 说明下一条语句的作用。
    listeners = listener_pids(port)
    # 说明下一条语句的作用。
    pid = recorded_pid()
    # 说明下一条语句的作用。
    return len(listeners) == 1 and pid in listeners and all(
        # 说明下一条语句的作用。
        is_dashboard_process(item) for item in listeners
    # 说明下一条语句的作用。
    )


# 说明下一条语句的作用。
def stop_previous_process(port: int) -> None:
    """停止本项目占用端口的旧 Dashboard，并拒绝终止外部进程。

    函数先区分 Dashboard 与非本项目监听者；发现外部监听者立即报错。合法目标收到
    终止信号后最多等待五秒，仍未退出则中止恢复，避免强杀未知状态进程。
    """
    # 说明下一条语句的作用。
    pid = recorded_pid()
    # 说明下一条语句的作用。
    listeners = listener_pids(port)
    # 说明下一条语句的作用。
    targets = {item for item in listeners if is_recoverable_dashboard_process(item)}
    # 说明下一条语句的作用。
    foreign_listeners = listeners - targets
    # 说明下一条语句的作用。
    if foreign_listeners:
        # 说明下一条语句的作用。
        raise RuntimeError(
            # 说明下一条语句的作用。
            "Dashboard 端口被非本项目进程占用: "
            # 说明下一条语句的作用。
            + ", ".join(str(item) for item in sorted(foreign_listeners))
        # 说明下一条语句的作用。
        )
    # 说明下一条语句的作用。
    if pid is not None and is_recoverable_dashboard_process(pid):
        # 说明下一条语句的作用。
        targets.add(pid)
    # 说明下一条语句的作用。
    for target in targets:
        # 说明下一条语句的作用。
        try:
            # 说明下一条语句的作用。
            os.kill(target, signal.SIGTERM)
        # 说明下一条语句的作用。
        except OSError:
            # 说明下一条语句的作用。
            pass
    # 说明下一条语句的作用。
    deadline = time.monotonic() + 5.0
    # 说明下一条语句的作用。
    while any(process_alive(target) for target in targets) and time.monotonic() < deadline:
        # 说明下一条语句的作用。
        time.sleep(0.1)
    # 说明下一条语句的作用。
    remaining = sorted(target for target in targets if process_alive(target))
    # 说明下一条语句的作用。
    if remaining:
        # 说明下一条语句的作用。
        raise RuntimeError(
            # 说明下一条语句的作用。
            "Dashboard Server 进程未在停止信号后退出: "
            # 说明下一条语句的作用。
            + ", ".join(str(item) for item in remaining)
        # 说明下一条语句的作用。
        )
    # 说明下一条语句的作用。
    for _ in range(20):
        # 说明下一条语句的作用。
        try:
            # 说明下一条语句的作用。
            PID_PATH.unlink(missing_ok=True)
            # 说明下一条语句的作用。
            return
        # 说明下一条语句的作用。
        except PermissionError:
            # 说明下一条语句的作用。
            time.sleep(0.1)
    # 说明下一条语句的作用。
    PID_PATH.unlink(missing_ok=True)


# 说明下一条语句的作用。
def start_server(database_path: Path, config_path: Path, port: int) -> int:
    """清理旧实例后在后台启动 Supervisor 主进程，并追加服务日志。"""
    # 说明下一条语句的作用。
    stop_previous_process(port)
    # 说明下一条语句的作用。
    log_stream = SERVER_LOG.open("a", encoding="utf-8", newline="\n")
    # 说明下一条语句的作用。
    creation_flags = 0
    # 说明下一条语句的作用。
    if os.name == "nt":
        # 说明下一条语句的作用。
        creation_flags = (
            # 说明下一条语句的作用。
            subprocess.CREATE_NEW_PROCESS_GROUP
            # 说明下一条语句的作用。
            | subprocess.DETACHED_PROCESS
            # 说明下一条语句的作用。
            | subprocess.CREATE_NO_WINDOW
        # 说明下一条语句的作用。
        )
    # 说明下一条语句的作用。
    process = subprocess.Popen(
        # 说明下一条语句的作用。
        [
            # 说明下一条语句的作用。
            sys.executable,
            # 说明下一条语句的作用。
            "-B",
            # 说明下一条语句的作用。
            str(BASE_DIR / "supervisor" / "main.py"),
            # 说明下一条语句的作用。
            "serve",
            # 说明下一条语句的作用。
            "--db",
            # 说明下一条语句的作用。
            str(database_path),
            # 说明下一条语句的作用。
            "--config",
            # 说明下一条语句的作用。
            str(config_path),
        # 说明下一条语句的作用。
        ],
        # 说明下一条语句的作用。
        cwd=str(BASE_DIR),
        # 说明下一条语句的作用。
        stdin=subprocess.DEVNULL,
        # 说明下一条语句的作用。
        stdout=log_stream,
        # 说明下一条语句的作用。
        stderr=log_stream,
        # 说明下一条语句的作用。
        close_fds=True,
        # 说明下一条语句的作用。
        creationflags=creation_flags,
    # 说明下一条语句的作用。
    )
    # 说明下一条语句的作用。
    log_stream.close()
    # 说明下一条语句的作用。
    return process.pid


# 说明下一条语句的作用。
def record(status: str, message: str, failures: int, pid: int | None = None) -> None:
    """更新健康快照并只保留最近 100 条事件；失败时写入后备日志。"""
    # 说明下一条语句的作用。
    try:
        # 说明下一条语句的作用。
        state = read_state()
        # 说明下一条语句的作用。
        monitors = state.get("monitors")
        # 说明下一条语句的作用。
        checked_at = now_shanghai()
        # 说明下一条语句的作用。
        events = list(state.get("events") or [])
        # 说明下一条语句的作用。
        events.insert(
            # 说明下一条语句的作用。
            0,
            # 说明下一条语句的作用。
            {
                # 说明下一条语句的作用。
                "at": checked_at,
                # 说明下一条语句的作用。
                "component": "dashboard-server",
                # 说明下一条语句的作用。
                "status": status,
                # 说明下一条语句的作用。
                "message": message,
                # 说明下一条语句的作用。
                "details": {"pid": pid, "failures": failures},
            # 说明下一条语句的作用。
            },
        # 说明下一条语句的作用。
        )
        # 说明下一条语句的作用。
        value = {
                # 说明下一条语句的作用。
                "component": "dashboard-server",
                # 说明下一条语句的作用。
                "status": status,
                # 说明下一条语句的作用。
                "pid": pid,
                # 说明下一条语句的作用。
                "checked_at": checked_at,
                # 说明下一条语句的作用。
                "last_checked_at": checked_at,
                # 说明下一条语句的作用。
                "consecutive_failures": failures,
                # 说明下一条语句的作用。
                "message": message,
                # 说明下一条语句的作用。
                "events": events[:100],
            # 说明下一条语句的作用。
            }
        # 说明下一条语句的作用。
        if isinstance(monitors, dict):
            # 说明下一条语句的作用。
            value["monitors"] = monitors
        # 说明下一条语句的作用。
        write_state(value)
    # 说明下一条语句的作用。
    except Exception as error:
        # 说明下一条语句的作用。
        append_fallback(f"{status} {message}; runtime state write failed: {error}")


# 说明下一条语句的作用。
def main(argv: list[str] | None = None) -> None:
    """执行一次探活、必要恢复、重试验证和阈值告警流程。

    健康且拓扑干净时直接返回。否则连续失败数加一并尝试启动；恢复成功会清零失败数，
    恢复失败则按配置阈值返回 ``UNHEALTHY`` 或 ``NEEDS_ATTENTION``。
    """
    # 说明下一条语句的作用。
    parser = argparse.ArgumentParser(description="Local Agent Loop Supervisor health check")
    # 说明下一条语句的作用。
    parser.add_argument("--db", default=str(DEFAULT_DB))
    # 说明下一条语句的作用。
    parser.add_argument("--config", default=str(CONFIG_PATH))
    # 说明下一条语句的作用。
    args = parser.parse_args(argv)
    # 说明下一条语句的作用。
    database_path = Path(args.db).resolve()
    # 说明下一条语句的作用。
    config_path = Path(args.config).resolve()
    # 说明下一条语句的作用。
    acquire_lock()
    # 说明下一条语句的作用。
    try:
        # 说明下一条语句的作用。
        config = load_initialization_config(config_path)
        # 说明下一条语句的作用。
        host = str(config["dashboard"]["host"])
        # 说明下一条语句的作用。
        port = int(config["dashboard"]["port"])
        # 说明下一条语句的作用。
        threshold = int(config["health"]["failure_threshold"])
        # 说明下一条语句的作用。
        url = f"http://{host}:{port}/healthz"
        # 说明下一条语句的作用。
        current = health_request(url)
        # 说明下一条语句的作用。
        if current and current.get("ok") and dashboard_topology_is_clean(port):
            # 说明下一条语句的作用。
            pid = int(PID_PATH.read_text(encoding="utf-8")) if PID_PATH.exists() else None
            # 说明下一条语句的作用。
            record("HEALTHY", "Supervisor 主进程正常。", 0, pid)
            # 说明下一条语句的作用。
            output({"outcome": "HEALTHY", "url": url, "health": current, "pid": pid})

        # 说明下一条语句的作用。
        state = read_state()
        # 说明下一条语句的作用。
        failures = int(state.get("consecutive_failures", 0)) + 1
        # 说明下一条语句的作用。
        try:
            # 说明下一条语句的作用。
            pid = start_server(database_path, config_path, port)
        # 说明下一条语句的作用。
        except (OSError, RuntimeError) as error:
            # 说明下一条语句的作用。
            status = "NEEDS_ATTENTION" if failures >= threshold else "UNHEALTHY"
            # 说明下一条语句的作用。
            message = f"Supervisor 主进程恢复启动失败：{error}"
            # 说明下一条语句的作用。
            record(status, message, failures)
            # 说明下一条语句的作用。
            output(
                # 说明下一条语句的作用。
                {
                    # 说明下一条语句的作用。
                    "outcome": status,
                    # 说明下一条语句的作用。
                    "url": url,
                    # 说明下一条语句的作用。
                    "message": message,
                    # 说明下一条语句的作用。
                    "consecutive_failures": failures,
                    # 说明下一条语句的作用。
                    "threshold": threshold,
                # 说明下一条语句的作用。
                },
                # 说明下一条语句的作用。
                2 if status == "NEEDS_ATTENTION" else 1,
            # 说明下一条语句的作用。
            )
        # 说明下一条语句的作用。
        recovered = None
        # 说明下一条语句的作用。
        for _ in range(20):
            # 说明下一条语句的作用。
            time.sleep(0.5)
            # 说明下一条语句的作用。
            recovered = health_request(url, timeout=1.0)
            # 说明下一条语句的作用。
            if recovered and recovered.get("ok"):
                # 说明下一条语句的作用。
                break
        # 说明下一条语句的作用。
        if recovered and recovered.get("ok"):
            # 说明下一条语句的作用。
            record("RESTARTED", "Supervisor 主进程已启动或恢复。", 0, pid)
            # 说明下一条语句的作用。
            output({"outcome": "RESTARTED", "url": url, "pid": pid, "health": recovered})
        # 说明下一条语句的作用。
        status = "NEEDS_ATTENTION" if failures >= threshold else "UNHEALTHY"
        # 说明下一条语句的作用。
        message = (
            # 说明下一条语句的作用。
            "Supervisor 主进程连续恢复失败，已达到告警阈值。"
            # 说明下一条语句的作用。
            if status == "NEEDS_ATTENTION"
            # 说明下一条语句的作用。
            else "Supervisor 主进程启动后仍不可用。"
        # 说明下一条语句的作用。
        )
        # 说明下一条语句的作用。
        record(status, message, failures, pid)
        # 说明下一条语句的作用。
        output(
            # 说明下一条语句的作用。
            {
                # 说明下一条语句的作用。
                "outcome": status,
                # 说明下一条语句的作用。
                "url": url,
                # 说明下一条语句的作用。
                "pid": pid,
                # 说明下一条语句的作用。
                "consecutive_failures": failures,
                # 说明下一条语句的作用。
                "threshold": threshold,
            # 说明下一条语句的作用。
            },
            # 说明下一条语句的作用。
            2 if status == "NEEDS_ATTENTION" else 1,
        # 说明下一条语句的作用。
        )
    # 说明下一条语句的作用。
    finally:
        # 说明下一条语句的作用。
        release_lock()


# 说明下一条语句的作用。
if __name__ == "__main__":
    # 说明下一条语句的作用。
    main()
