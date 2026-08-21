"""角色调度入口共用的 Windows 后台进程启动方法。"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import IO, Any


DETACHED_PROCESS_FLAGS = (
    subprocess.CREATE_NEW_PROCESS_GROUP
    | subprocess.DETACHED_PROCESS
    | subprocess.CREATE_NO_WINDOW
)
SENSITIVE_ENVIRONMENT_NAME = re.compile(
    r"(secret|credential|password|api[_-]?key|access[_-]?token|private[_-]?key|authorization)",
    re.IGNORECASE,
)


def safe_process_environment() -> dict[str, str]:
    """返回不含名称表明为凭据的子进程环境副本。"""
    return {
        name: value
        for name, value in os.environ.items()
        if name.casefold() != "codex_home"
        and not SENSITIVE_ENVIRONMENT_NAME.search(name)
    }


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
