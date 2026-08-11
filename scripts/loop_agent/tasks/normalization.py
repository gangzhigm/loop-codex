"""Canonical validation for task text lists and value-free diagnostics.

These functions are intentionally independent of SQLite. Operator, Planner,
Worker, migrations, and database projections all use the same strict shapes,
which prevents one entry point from accepting data another cannot read back.
All diagnostic metadata is value-free: no prompt, response, or credential text
is stored in the task database.
"""

from __future__ import annotations

# 中文排查：字符串列表、结果诊断和 Planner 拆分建议在这里做无副作用的形状规范化。
# 字段被拒绝时按顶层类型、必填键、允许值和嵌套元素顺序检查。
# 规范化函数不访问数据库；相同输入必须产生相同结果，方便单独复现数据问题。

import json
import re
from typing import Any

from loop_agent.constants import CAPABILITY_LEVELS
from loop_agent.errors import LoopError
from loop_agent.serialization import json_dump


RESULT_DIAGNOSTIC_CATEGORIES = {
    "authentication",
    "connection",
    "empty_or_malformed_response",
    "final_schema",
    "invalid_final_json",
    "invalid_tool_call",
    "local_protocol",
    "rate_limited",
    "request_invalid",
    "request_timeout",
    "server_error",
    "truncated_response",
    "unsupported_finish_reason",
}
RESULT_DIAGNOSTIC_FINISH_REASONS = {
    "length",
    "content_filter",
    "insufficient_system_resource",
    "stop",
    "tool_calls",
}
RESULT_DIAGNOSTIC_FIELD_NAMES = (
    "status",
    "summary",
    "verification",
    "completed",
    "error",
    "question",
    "options",
    "result",
    "message",
    "output",
)
RESULT_DIAGNOSTIC_TYPE_TAGS = {
    "array",
    "boolean",
    "null",
    "number",
    "object",
    "string",
    "unavailable",
}
RESULT_DIAGNOSTIC_PARSE_STATES = {"invalid_json", "parsed", "unavailable"}
TRANSIENT_RESULT_DIAGNOSTIC_CATEGORIES = {
    "connection",
    "rate_limited",
    "request_timeout",
    "server_error",
}


def normalize_result_diagnostic(raw: Any) -> dict[str, Any] | None:
    """Validate and canonicalize value-free result diagnostics."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LoopError("result diagnostic 必须是对象")
    allowed = {
        "category",
        "http_status",
        "retryable",
        "retry_exhausted",
        "finish_reason",
        "agent_attempt",
        "model_step",
        "final_shape",
    }
    if set(raw) - allowed:
        raise LoopError("result diagnostic 包含未知字段")
    category = raw.get("category")
    if category not in RESULT_DIAGNOSTIC_CATEGORIES:
        raise LoopError("result diagnostic category 无效")
    http_status = raw.get("http_status")
    if http_status is not None and (
        not isinstance(http_status, int)
        or isinstance(http_status, bool)
        or not 100 <= http_status <= 599
    ):
        raise LoopError("result diagnostic HTTP status 无效")
    retryable = raw.get("retryable")
    retry_exhausted = raw.get("retry_exhausted")
    if not isinstance(retryable, bool) or not isinstance(retry_exhausted, bool):
        raise LoopError("result diagnostic retry 字段无效")
    if retryable and category not in TRANSIENT_RESULT_DIAGNOSTIC_CATEGORIES:
        raise LoopError("result diagnostic category 不允许重试")
    if retry_exhausted and not retryable:
        raise LoopError("result diagnostic retry_exhausted 无效")
    finish_reason = raw.get("finish_reason")
    if (
        finish_reason is not None
        and finish_reason not in RESULT_DIAGNOSTIC_FINISH_REASONS
    ):
        raise LoopError("result diagnostic finish_reason 无效")
    attempts: dict[str, int | None] = {}
    for key in ("agent_attempt", "model_step"):
        value = raw.get(key)
        if value is not None and (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 1 <= value <= 10_000
        ):
            raise LoopError(f"result diagnostic {key} 无效")
        attempts[key] = value
    final_shape = _normalize_final_shape(raw.get("final_shape"))
    if final_shape is not None and finish_reason != "stop":
        raise LoopError("result diagnostic final_shape 需要 stop")
    if final_shape is not None and category not in {
        "final_schema",
        "invalid_final_json",
    }:
        raise LoopError("result diagnostic category 不允许 final_shape")
    result: dict[str, Any] = {
        "category": category,
        "http_status": http_status,
        "retryable": retryable,
        "retry_exhausted": retry_exhausted,
        "finish_reason": finish_reason,
        **attempts,
    }
    if final_shape is not None:
        result["final_shape"] = final_shape
    return result


def _normalize_final_shape(raw: Any) -> dict[str, Any] | None:
    """Validate final-response shape metadata without retaining field values."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {
        "finish_reason",
        "content_length",
        "json_parse_state",
        "top_level_type",
        "allowed_fields",
        "unknown_field_count",
        "unknown_fields_present",
    }:
        raise LoopError("result diagnostic final_shape 无效")
    if raw.get("finish_reason") != "stop":
        raise LoopError("result diagnostic shape finish_reason 无效")
    content_length = raw.get("content_length")
    unknown_count = raw.get("unknown_field_count")
    unknown_present = raw.get("unknown_fields_present")
    if (
        not isinstance(content_length, int)
        or isinstance(content_length, bool)
        or not 0 <= content_length <= 10_000_000
    ):
        raise LoopError("result diagnostic content_length 无效")
    if (
        not isinstance(unknown_count, int)
        or isinstance(unknown_count, bool)
        or not 0 <= unknown_count <= 10_000
    ):
        raise LoopError("result diagnostic unknown_field_count 无效")
    if not isinstance(unknown_present, bool) or unknown_present != (
        unknown_count > 0
    ):
        raise LoopError("result diagnostic unknown_fields_present 无效")
    parse_state = raw.get("json_parse_state")
    top_level_type = raw.get("top_level_type")
    if (
        parse_state not in RESULT_DIAGNOSTIC_PARSE_STATES
        or top_level_type not in RESULT_DIAGNOSTIC_TYPE_TAGS
    ):
        raise LoopError("result diagnostic shape 类型无效")
    fields = raw.get("allowed_fields")
    if not isinstance(fields, dict) or set(fields) != set(
        RESULT_DIAGNOSTIC_FIELD_NAMES
    ):
        raise LoopError("result diagnostic allowed_fields 无效")
    normalized_fields: dict[str, dict[str, Any]] = {}
    for name in RESULT_DIAGNOSTIC_FIELD_NAMES:
        field = fields[name]
        if not isinstance(field, dict) or set(field) != {"present", "type"}:
            raise LoopError("result diagnostic field metadata 无效")
        present = field.get("present")
        value_type = field.get("type")
        if (
            not isinstance(present, bool)
            or value_type not in RESULT_DIAGNOSTIC_TYPE_TAGS
        ):
            raise LoopError("result diagnostic field type 无效")
        if present == (value_type == "unavailable"):
            raise LoopError("result diagnostic field presence/type 不一致")
        normalized_fields[name] = {"present": present, "type": value_type}
    if parse_state == "parsed" and top_level_type == "unavailable":
        raise LoopError("result diagnostic parsed top-level type 无效")
    if parse_state != "parsed" and (
        top_level_type != "unavailable"
        or unknown_count != 0
        or any(field["present"] for field in normalized_fields.values())
    ):
        raise LoopError("result diagnostic unparsed shape 无效")
    return {
        "finish_reason": "stop",
        "content_length": content_length,
        "json_parse_state": parse_state,
        "top_level_type": top_level_type,
        "allowed_fields": normalized_fields,
        "unknown_field_count": unknown_count,
        "unknown_fields_present": unknown_present,
    }


def normalize_string_list(
    raw: Any, field: str, *, allow_empty: bool = True
) -> list[str]:
    """Normalize an ordered, unique list of non-empty strings."""
    if not isinstance(raw, list):
        raise LoopError(f"{field} 必须是数组")
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise LoopError(f"{field} 只能包含非空字符串")
        values.append(item.strip())
    if len(values) != len(set(values)):
        raise LoopError(f"{field} 不能包含重复项")
    if not allow_empty and not values:
        raise LoopError(f"{field} 不能为空")
    return values


def normalize_split_suggestions(raw: Any) -> list[dict[str, Any]]:
    """Validate Planner suggestions without creating any child tasks."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise LoopError("split_suggestions 必须是数组")
    suggestions: list[dict[str, Any]] = []
    for suggestion in raw:
        if not isinstance(suggestion, dict) or set(suggestion) != {
            "reason",
            "tasks",
        }:
            raise LoopError("split_suggestions 项必须只包含 reason 和 tasks")
        reason = suggestion.get("reason")
        tasks = suggestion.get("tasks")
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or not isinstance(tasks, list)
            or not tasks
        ):
            raise LoopError("split_suggestions 的 reason 和 tasks 不能为空")
        normalized_tasks: list[dict[str, Any]] = []
        identifiers: set[str] = set()
        for proposed in tasks:
            required = {
                "id",
                "title",
                "description",
                "scope",
                "capability_level",
                "depends_on",
                "parallel_with",
            }
            if not isinstance(proposed, dict) or set(proposed) != required:
                raise LoopError("拆分子任务字段不完整")
            task_id = proposed.get("id")
            title = proposed.get("title")
            description = proposed.get("description")
            capability = proposed.get("capability_level")
            if not isinstance(task_id, str) or not re.fullmatch(
                r"[A-Z][A-Z0-9_-]*", task_id
            ):
                raise LoopError("拆分子任务 id 无效")
            if task_id in identifiers:
                raise LoopError("拆分子任务 id 不能重复")
            identifiers.add(task_id)
            if not isinstance(title, str) or not title.strip():
                raise LoopError("拆分子任务 title 不能为空")
            if not isinstance(description, str) or not description.strip():
                raise LoopError("拆分子任务 description 不能为空")
            if capability not in CAPABILITY_LEVELS:
                raise LoopError("拆分子任务 capability_level 无效")
            normalized_tasks.append(
                {
                    "id": task_id,
                    "title": title.strip(),
                    "description": description.strip(),
                    "scope": normalize_string_list(
                        proposed.get("scope"),
                        "拆分子任务 scope",
                        allow_empty=False,
                    ),
                    "capability_level": capability,
                    "depends_on": normalize_string_list(
                        proposed.get("depends_on"), "拆分子任务 depends_on"
                    ),
                    "parallel_with": normalize_string_list(
                        proposed.get("parallel_with"), "拆分子任务 parallel_with"
                    ),
                }
            )
        suggestions.append(
            {"reason": reason.strip(), "tasks": normalized_tasks}
        )
    return suggestions


def load_result_diagnostic(value: str | None) -> dict[str, Any] | None:
    """Load only diagnostics already stored in canonical JSON form."""
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError) as error:
        raise LoopError("result diagnostic JSON 无效") from error
    normalized = normalize_result_diagnostic(parsed)
    if normalized is None or json_dump(normalized) != value:
        raise LoopError("result diagnostic 不是规范 JSON")
    return normalized
