"""仅支持 Windows 的 Supervisor 单次健康检查与受控恢复程序。

脚本由计划任务周期调用，不常驻轮询。每次运行先取得文件锁，再核对 Supervisor PID、
实际进程身份和主循环 heartbeat；主进程缺失或 heartbeat 过期时才尝试清理旧实例并
启动新的 Supervisor 主进程。检查结果写入有限长度的健康状态文件，写入失败则退化到文本日志。

本程序只管理 Supervisor 主进程（其承载 Dashboard 并运行组件监控），不领取任务、不回收 Worker execution，
也不会把“进程消失”解释为任务已经安全结束。
"""

from __future__ import annotations

# 健康任务负责探活 Supervisor、维护 PID/健康 JSON，并在阈值满足时恢复主进程。
# 它不领取 AI 任务，也不能根据健康信号推断任何 Worker 会话已经安全结束。
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


# 运行健康检查和后台服务时不生成 __pycache__，避免产生无关运行文件。
sys.dont_write_bytecode = True

# loopdb 位于 control 目录；计划任务直接运行本文件时需要显式加入搜索路径。
CONTROL_ROOT = Path(__file__).resolve().parents[1] / "control"
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))

# 复用控制面的权威路径、配置校验、JSON 序列化和上海时区时间函数。
from loopdb import (
    BASE_DIR,
    CONFIG_PATH,
    DEFAULT_DB,
    json_dump,
    load_initialization_config,
    now_shanghai,
)


# 所有 Supervisor 运行状态集中在 runtime，任务事实仍只保存在 SQLite。
RUNTIME_DIR = BASE_DIR / "runtime"
# 互斥锁阻止多个计划任务实例同时执行恢复流程。
HEALTH_LOCK = RUNTIME_DIR / "health-supervisor.lock"
# 健康快照是 Dashboard 展示服务状态的文件来源。
HEALTH_STATE = RUNTIME_DIR / "health-state.json"
# PID 文件记录当前 Supervisor 主进程，不借用 Dashboard 线程的运行文件。
PID_PATH = RUNTIME_DIR / "supervisor.pid"
# 主循环每完成一轮组件监控就刷新 heartbeat，供外部健康任务判断是否仍在推进。
HEARTBEAT_PATH = RUNTIME_DIR / "supervisor-heartbeat.json"
# 健康快照写入失败时，只把诊断信息追加到后备日志。
FALLBACK_LOG = RUNTIME_DIR / "health-fallback.log"
# 恢复出的常驻 Supervisor 将标准输出和错误输出追加到该日志。
SERVER_LOG = RUNTIME_DIR / "supervisor.log"


def output(payload: dict[str, Any], exit_code: int = 0) -> None:
    """输出 UTF-8 JSON 结果并以指定退出码立即结束本次健康检查。"""
    # 保留中文诊断，便于从计划任务历史直接阅读结果。
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(exit_code)


def append_fallback(message: str) -> None:
    """健康状态 JSON 无法写入时，将最小诊断信息追加到后备日志。"""
    # 首次运行时 runtime 可能不存在，因此写日志前幂等创建目录。
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
        # 损坏状态不能阻止恢复流程，失败计数从安全初始值重新开始。
        return {"consecutive_failures": 0, "last_checked_at": None, "events": []}


def write_state(value: dict[str, Any]) -> None:
    """先写临时文件再原子替换健康状态，避免中断留下半截 JSON。"""
    # 临时文件与目标位于同一目录，replace 可在同一文件系统内原子完成。
    temporary = HEALTH_STATE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(HEALTH_STATE)


def acquire_lock() -> None:
    """建立健康检查互斥锁，并清理超过 120 秒的遗留锁。"""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    if HEALTH_LOCK.exists():
        # 120 秒以内的锁视为另一健康检查仍在运行。
        age = time.time() - HEALTH_LOCK.stat().st_mtime
        if age < 120:
            output({"outcome": "BUSY", "message": "health supervisor already running"})
        # 超时锁视为异常退出遗留，清理后重新竞争。
        HEALTH_LOCK.unlink()

    # O_EXCL 保证检查与创建是原子的，避免两个实例同时取得锁。
    descriptor = os.open(HEALTH_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(json_dump({"pid": os.getpid(), "started_at": now_shanghai()}))


def release_lock() -> None:
    """幂等删除当前健康检查锁；锁已不存在时不报错。"""
    try:
        HEALTH_LOCK.unlink()
    except FileNotFoundError:
        # 所有退出路径都会进入 finally，因此容忍锁已被清理。
        pass


def process_alive(pid: int) -> bool:
    """通过 Windows 进程退出码判断 PID 是否仍活动。"""
    # Windows 没有等价的 os.kill(pid, 0)，改用受限查询句柄。
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
        # 无论查询是否成功都释放 Windows 进程句柄。
        kernel32.CloseHandle(handle)


def windows_powershell(command: str) -> str:
    """隐藏运行 PowerShell 查询命令，失败时返回空字符串。"""
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
    # 去除末尾换行，便于后续命令行路径匹配。
    return completed.stdout.strip()


def listener_pids(port: int) -> set[int]:
    """在 Windows 上从 netstat 提取指定 TCP 端口的全部监听 PID。"""
    # netstat 是端口拓扑事实来源，输出仅在内存中解析。
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

    # 只接受 TCP LISTENING 行，并精确匹配最后一个冒号后的端口号。
    for line in completed.stdout.splitlines():
        columns = line.split()
        if len(columns) < 5 or columns[0].upper() != "TCP" or columns[3].upper() != "LISTENING":
            continue
        if columns[1].rsplit(":", 1)[-1] != str(port):
            continue
        try:
            result.add(int(columns[4]))
        except ValueError:
            # 异常 PID 字段不可信，跳过该行继续检查其他监听者。
            continue
    return result


def is_supervisor_process(pid: int) -> bool:
    """确认 PID 存活且 Windows 命令行确实指向本项目 Supervisor 主入口。"""
    # PID 文件可能过期，先确认系统中仍存在该进程。
    if not process_alive(pid):
        return False

    # 核对完整命令行，避免仅凭 PID 误认复用后的外部进程。
    command_line = windows_powershell(
        f'(Get-CimInstance Win32_Process -Filter "ProcessId = {pid}").CommandLine'
    )
    supervisor_entry = str(BASE_DIR / "supervisor" / "main.py")
    # 同时要求入口路径和 serve 子命令，health 短进程不能冒充常驻服务。
    return (
        supervisor_entry.casefold() in command_line.casefold()
        and "serve" in command_line.casefold()
    )


def recorded_pid() -> int | None:
    """读取 PID 文件；缺失、不可读或非整数内容都视为没有可信记录。"""
    if not PID_PATH.exists():
        return None
    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        # 不可信 PID 不能用于终止进程。
        return None


def read_supervisor_heartbeat() -> dict[str, Any] | None:
    """读取主循环 heartbeat；文件缺失、损坏或字段非法时返回 ``None``。"""
    try:
        value = json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return None
        return {"pid": int(value["pid"]), "checked_at": str(value["checked_at"])}
    except (FileNotFoundError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def supervisor_health(
    heartbeat_timeout_seconds: int,
    expected_pid: int | None = None,
) -> tuple[int | None, dict[str, Any] | None]:
    """核对 PID、进程身份及 heartbeat 新鲜度，返回可信进程和 heartbeat。"""
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
    """停止本项目的旧 Supervisor，并拒绝终止占用 Dashboard 端口的外部进程。"""
    # Supervisor PID 和监听列表从两个独立角度收集可能需要停止的实例。
    pid = recorded_pid()
    listeners = listener_pids(port)
    targets = {item for item in listeners if is_supervisor_process(item)}
    foreign_listeners = listeners - targets
    if foreign_listeners:
        # 端口存在外部进程时禁止恢复，避免误杀其他应用。
        raise RuntimeError(
            "Dashboard 端口被非本项目进程占用: "
            + ", ".join(str(item) for item in sorted(foreign_listeners))
        )
    if pid is not None and is_supervisor_process(pid):
        targets.add(pid)

    # 只向已验证属于本项目的进程发送正常终止信号。
    for target in targets:
        try:
            os.kill(target, signal.SIGTERM)
        except OSError:
            # 目标可能在检查后自行退出，继续处理其余目标。
            pass

    # 最多等待五秒，不使用强杀掩盖无法安全终止的进程。
    deadline = time.monotonic() + 5.0
    while any(process_alive(target) for target in targets) and time.monotonic() < deadline:
        time.sleep(0.1)
    remaining = sorted(target for target in targets if process_alive(target))
    if remaining:
        raise RuntimeError(
            "Supervisor 主进程未在停止信号后退出: "
            + ", ".join(str(item) for item in remaining)
        )

    # Windows 可能短暂占用运行文件，有限重试后再做最终删除。
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
    """清理旧实例后在后台启动 Supervisor 主进程，并追加服务日志。"""
    # 先清理已验证的旧实例，确保新进程不会因端口占用立即退出。
    stop_previous_process(port)
    log_stream = SERVER_LOG.open("a", encoding="utf-8", newline="\n")
    # 后台进程脱离计划任务控制台运行，同时隐藏窗口并建立独立进程组。
    creation_flags = (
        subprocess.CREATE_NEW_PROCESS_GROUP
        | subprocess.DETACHED_PROCESS
        | subprocess.CREATE_NO_WINDOW
    )

    # 始终通过统一 main.py serve 入口启动，保持身份检查契约一致。
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
    # Popen 已复制日志句柄，父健康检查无需继续持有文件描述符。
    log_stream.close()
    return process.pid


def record(status: str, message: str, failures: int, pid: int | None = None) -> None:
    """更新健康快照并只保留最近 100 条事件；失败时写入后备日志。"""
    try:
        state = read_state()
        # 常驻 serve 写入的组件监控由 health 更新时原样保留。
        monitors = state.get("monitors")
        checked_at = now_shanghai()
        events = list(state.get("events") or [])

        # 新事件插到列表开头，Dashboard 总是优先展示最近结果。
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
        # 主状态文件不可写时，把公开消息留在后备日志供人工排查。
        append_fallback(f"{status} {message}; runtime state write failed: {error}")


def main(argv: list[str] | None = None) -> None:
    """执行一次探活、必要恢复、重试验证和阈值告警流程。"""
    parser = argparse.ArgumentParser(description="Local Agent Loop Supervisor health check")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args(argv)
    database_path = Path(args.db).resolve()
    config_path = Path(args.config).resolve()

    # 锁覆盖配置读取、探活和恢复全过程，防止并发重启。
    acquire_lock()
    try:
        config = load_initialization_config(config_path)
        port = int(config["dashboard"]["port"])
        threshold = int(config["health"]["failure_threshold"])
        heartbeat_timeout = int(config["health"]["heartbeat_timeout_seconds"])
        pid, heartbeat = supervisor_health(heartbeat_timeout)

        # 主进程身份和主循环 heartbeat 必须同时可信，才判定 Supervisor 健康。
        if pid is not None:
            record("HEALTHY", "Supervisor 主进程正常。", 0, pid)
            output({"outcome": "HEALTHY", "pid": pid, "heartbeat": heartbeat})

        # 进程身份或 heartbeat 异常都累计一次失败，并尝试受控恢复。
        state = read_state()
        failures = int(state.get("consecutive_failures", 0)) + 1
        try:
            pid = start_server(database_path, config_path, port)
        except (OSError, RuntimeError) as error:
            # 连续失败达到配置阈值后升级为需要人工关注。
            status = "NEEDS_ATTENTION" if failures >= threshold else "UNHEALTHY"
            message = f"Supervisor 主进程恢复启动失败：{error}"
            record(status, message, failures)
            output(
                {
                    "outcome": status,
                    "message": message,
                    "consecutive_failures": failures,
                    "threshold": threshold,
                },
                2 if status == "NEEDS_ATTENTION" else 1,
            )

        # 新进程最多等待三十秒完成首轮监控并写入属于该 PID 的 heartbeat。
        recovered_heartbeat = None
        for _ in range(60):
            time.sleep(0.5)
            recovered_pid, recovered_heartbeat = supervisor_health(
                heartbeat_timeout,
                expected_pid=pid,
            )
            if recovered_pid is not None:
                break

        # 恢复成功后失败计数清零，并返回新进程 PID。
        if recovered_pid is not None:
            record("RESTARTED", "Supervisor 主进程已启动或恢复。", 0, pid)
            output(
                {
                    "outcome": "RESTARTED",
                    "pid": pid,
                    "heartbeat": recovered_heartbeat,
                }
            )

        # 进程已启动但始终无法通过探活，继续保留连续失败计数。
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
                "pid": pid,
                "consecutive_failures": failures,
                "threshold": threshold,
            },
            2 if status == "NEEDS_ATTENTION" else 1,
        )
    finally:
        # 包括 output() 抛出 SystemExit 的所有路径都必须释放互斥锁。
        release_lock()


if __name__ == "__main__":
    main()
