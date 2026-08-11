"""UTF-8 command input/output and optimistic-concurrency helpers.

Every loopctl command returns one JSON object on stdout. Planner and Worker
reports use stdin so command lines never contain large or sensitive payloads.
The integrity check rejects unmistakably corrupted Planner text before it can
become task history.
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
    """Write one human-readable UTF-8 JSON response and optionally exit."""
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if exit_code:
        raise SystemExit(exit_code)


def read_json(path: Path) -> Any:
    """Read a UTF-8 JSON file without permissive replacement decoding."""
    return json.loads(path.read_text(encoding="utf-8"))


def read_json_source(source: str) -> Any:
    """Read UTF-8 JSON from stdin when source is '-', otherwise from a file."""
    if source == "-":
        return json.loads(sys.stdin.read())
    return read_json(Path(source).resolve())


def read_preflight_report(source: str) -> Any:
    """Enforce the Planner's host-controlled stdin-only writeback boundary."""
    if source != "-":
        raise LoopError("Planner 预检结果只允许通过 UTF-8 stdin 提交")
    return read_json_source(source)


def validate_preflight_text_integrity(value: Any, field: str = "payload") -> None:
    """Reject unmistakable UTF-8 writeback corruption before SQLite writes."""
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
    """Reject stale operator actions when a row changed after it was displayed."""
    expected = getattr(args, "expected_row_version", None)
    if expected is not None and expected != actual:
        raise LoopError(f"任务已发生并发变化：expected row_version={expected}, actual={actual}")
