"""项目长期运行入口可复用的 Windows 进程和端口查询方法。"""

from __future__ import annotations

import ctypes
import subprocess


def process_alive(pid: int) -> bool:
    """通过 Windows 进程退出码判断 PID 是否仍活动。"""
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


def listener_pids(port: int) -> set[int]:
    """从 Windows netstat 输出中提取指定 TCP 监听端口的 PID。"""
    try:
        completed = subprocess.run(
            ["netstat.exe", "-ano", "-p", "tcp"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
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
