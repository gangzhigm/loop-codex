"""UTF-8 命令输入输出与乐观并发辅助函数。

每个 loopctl 命令都在 stdout 返回一个 JSON 对象。Planner 和 Worker 报告通过 stdin 提交，
避免命令行包含大段或敏感 payload。完整性检查会在明显损坏的 Planner 文本进入任务历史前
将其拒绝。
"""

from __future__ import annotations

# 中文排查：控制面所有 JSON 输出、UTF-8 stdin 输入和 row_version 乐观锁检查集中在这里。
# 出现乱码或 payload 拒绝时先检查调用方是否用 UTF-8 stdin，并确认没有替换字符或连续问号。
# 不要放宽完整性检查来接收损坏文本，损坏数据一旦写入任务历史很难安全修复。

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from loop_agent.errors import LoopError


SUSPICIOUS_QUESTION_MARK_RUN = re.compile(r"\?{4,}")


def output(payload: dict[str, Any], exit_code: int = 0) -> None:
    """写出一份便于人工阅读的 UTF-8 JSON 响应，并按需退出。"""
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if exit_code:
        raise SystemExit(exit_code)


def read_json(path: Path) -> Any:
    """读取 UTF-8 JSON 文件，不允许使用替换字符宽松解码。"""
    return json.loads(path.read_text(encoding="utf-8"))


def read_json_source(source: str) -> Any:
    """source 为 ``-`` 时从 stdin 读取 UTF-8 JSON，否则从文件读取。"""
    if source == "-":
        return json.loads(sys.stdin.read())
    return read_json(Path(source).resolve())


def read_preflight_report(source: str) -> Any:
    """强制 Planner 只能通过宿主控制的 stdin 边界写回。"""
    if source != "-":
        raise LoopError("Planner 预检结果只允许通过 UTF-8 stdin 提交")
    return read_json_source(source)


def validate_preflight_text_integrity(value: Any, field: str = "payload") -> None:
    """在写入 SQLite 前拒绝明显损坏的 UTF-8 写回内容。"""
    if isinstance(value, str):
        if "\ufffd" in value or SUSPICIOUS_QUESTION_MARK_RUN.search(value):
            raise LoopError(f"Planner UTF-8 写回文本损坏: {field}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            validate_preflight_text_integrity(item, f"{field}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            validate_preflight_text_integrity(item, f"{field}.{key}")


def require_expected_row_version(args: argparse.Namespace, actual: int) -> None:
    """记录在展示后发生变化时，拒绝基于旧版本的 Operator 操作。"""
    expected = getattr(args, "expected_row_version", None)
    if expected is not None and expected != actual:
        raise LoopError(f"任务已发生并发变化：expected row_version={expected}, actual={actual}")
