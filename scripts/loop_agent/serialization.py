"""UTF-8 JSON and Asia/Shanghai timestamp helpers.

SQLite stores timestamps as explicit ISO 8601 strings and structured values as
compact UTF-8 JSON. Centralizing those conversions prevents subtly different
formats in migrations, task writes, and execution results.
"""

from __future__ import annotations

# 中文排查：统一处理 Asia/Shanghai 时间和 UTF-8 JSON 序列化/反序列化。
# 时间排序异常先确认输入是否带时区；JSON 异常先检查默认值和数据库中的原始字段类型。
# 不要在调用方重复实现时间格式，否则任务历史、租约和 Dashboard 可能出现口径分裂。

import json
from datetime import datetime, timedelta, timezone
from typing import Any


SHANGHAI = timezone(timedelta(hours=8))


def now_shanghai() -> str:
    """Return the current Asia/Shanghai time with millisecond precision."""
    return datetime.now(SHANGHAI).isoformat(timespec="milliseconds")


def expires_at(seconds: int) -> str:
    """Return an Asia/Shanghai deadline relative to the current time."""
    return (datetime.now(SHANGHAI) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def json_dump(value: Any) -> str:
    """Serialize structured state without ASCII escaping or extra whitespace."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_load(value: str | None, default: Any) -> Any:
    """Decode stored JSON, returning the caller's default for empty columns."""
    if value is None or value == "":
        return default
    return json.loads(value)
