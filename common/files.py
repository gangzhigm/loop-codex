"""多个长期运行入口可复用的 UTF-8 运行文件方法。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_json_object(path: Path) -> dict[str, Any] | None:
    """读取 UTF-8 JSON 对象；文件或内容不可信时返回 ``None``。"""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    """先完整写入同目录临时文件，再原子替换目标 JSON。"""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def append_utf8_line(path: Path, message: str) -> None:
    """把一行诊断追加到 UTF-8 文本文件，不覆盖既有内容。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(message + "\n")


def read_pid(path: Path) -> int | None:
    """读取 PID 文件；缺失、不可读或非正整数都视为没有可信记录。"""
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
        return pid if pid > 0 else None
    except (FileNotFoundError, OSError, ValueError):
        return None


def heartbeat_belongs_to(path: Path, pid: int) -> bool:
    """确认 heartbeat 文件中的 PID 仍属于指定进程。"""
    value = read_json_object(path)
    if value is None:
        return False
    try:
        return int(value["pid"]) == pid
    except (KeyError, TypeError, ValueError):
        return False
