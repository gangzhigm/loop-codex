"""角色调度入口共用的 Windows 后台进程启动方法。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import IO, Any


DETACHED_PROCESS_FLAGS = (
    subprocess.CREATE_NEW_PROCESS_GROUP
    | subprocess.DETACHED_PROCESS
    | subprocess.CREATE_NO_WINDOW
)


def launch_detached_process(
    command: list[str],
    working_directory: Path,
    *,
    stdout: int | IO[Any] = subprocess.DEVNULL,
    stderr: int | IO[Any] = subprocess.DEVNULL,
) -> int:
    """启动与调度器解耦的后台进程，成功后只返回 PID。"""
    process = subprocess.Popen(
        command,
        cwd=working_directory,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        close_fds=True,
        creationflags=DETACHED_PROCESS_FLAGS,
    )
    return process.pid
