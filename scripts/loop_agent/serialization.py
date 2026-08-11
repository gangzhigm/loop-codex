"""UTF-8 JSON 与 Asia/Shanghai 时间戳辅助函数。

SQLite 使用显式 ISO 8601 字符串保存时间戳，并使用紧凑 UTF-8 JSON 保存结构化值。
集中处理这些转换可以避免迁移、任务写入和执行结果出现细微的格式差异。
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
    """返回精确到毫秒的当前 Asia/Shanghai 时间。"""
    return datetime.now(SHANGHAI).isoformat(timespec="milliseconds")


def expires_at(seconds: int) -> str:
    """返回相对当前时间计算的 Asia/Shanghai 截止时间。"""
    return (datetime.now(SHANGHAI) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def json_dump(value: Any) -> str:
    """序列化结构化状态，不转义 ASCII，也不添加多余空白。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_load(value: str | None, default: Any) -> Any:
    """解码已存储的 JSON；字段为空时返回调用方提供的默认值。"""
    if value is None or value == "":
        return default
    return json.loads(value)
