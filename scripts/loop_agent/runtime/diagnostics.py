"""模型和 Provider 失败使用的公开无值诊断。

只有这里定义的固定字段可以从模型 Provider 进入日志、任务结果或 Dashboard。原始响应、请求
正文、工具参数和密钥材料绝不能附加到这些对象中。
"""

from __future__ import annotations

# 中文排查：Provider 与 final 失败被压缩成不含值的稳定诊断结构。
# 类别错误时检查异常到 category 的映射；字段形状错误时检查 final_shape 的允许字段。
# 不得为方便排障加入原始内容、字段值、哈希或可用于推断敏感信息的片段。

from dataclasses import dataclass
from typing import Any


class AgentRuntimeError(RuntimeError):
    pass


DIAGNOSTIC_CATEGORIES = frozenset({
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
})
TRANSIENT_DIAGNOSTIC_CATEGORIES = frozenset({
    "connection",
    "rate_limited",
    "request_timeout",
    "server_error",
})
FINAL_SHAPE_FIELD_NAMES = (
    "status", "summary", "verification", "completed", "error", "question", "options", "result",
    "message", "output",
)
FINAL_SHAPE_TYPE_TAGS = frozenset({"array", "boolean", "null", "number", "object", "string", "unavailable"})
FINAL_SHAPE_PARSE_STATES = frozenset({"invalid_json", "parsed", "unavailable"})


def _shape_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unavailable"


@dataclass(frozen=True)
class FinalShapeDiagnostic:
    """被拒绝 final 响应使用的固定无值元数据。"""

    finish_reason: str
    content_length: int
    json_parse_state: str
    top_level_type: str
    allowed_fields: tuple[tuple[str, bool, str], ...]
    unknown_field_count: int

    def __post_init__(self) -> None:
        if self.finish_reason != "stop":
            raise ValueError("final shape diagnostics require finish_reason=stop")
        if self.content_length < 0 or self.json_parse_state not in FINAL_SHAPE_PARSE_STATES:
            raise ValueError("final shape diagnostic is invalid")
        if self.top_level_type not in FINAL_SHAPE_TYPE_TAGS:
            raise ValueError("final shape top-level type is invalid")
        if tuple(name for name, _present, _type in self.allowed_fields) != FINAL_SHAPE_FIELD_NAMES:
            raise ValueError("final shape fields must use the fixed allowlist")
        if any(not isinstance(present, bool) or value_type not in FINAL_SHAPE_TYPE_TAGS for _name, present, value_type in self.allowed_fields):
            raise ValueError("final shape field metadata is invalid")
        if self.unknown_field_count < 0:
            raise ValueError("final shape unknown field count is invalid")

    def as_dict(self) -> dict[str, Any]:
        return {
            "finish_reason": self.finish_reason,
            "content_length": self.content_length,
            "json_parse_state": self.json_parse_state,
            "top_level_type": self.top_level_type,
            "allowed_fields": {
                name: {"present": present, "type": value_type}
                for name, present, value_type in self.allowed_fields
            },
            "unknown_field_count": self.unknown_field_count,
            "unknown_fields_present": self.unknown_field_count > 0,
        }


def final_shape_diagnostic(
    raw: Any,
    *,
    content_length: int | None = None,
    json_parse_state: str = "parsed",
) -> FinalShapeDiagnostic:
    """汇总 final 内容，但不保留字段值或未知字段名。"""
    if content_length is None:
        content_length = len(raw) if isinstance(raw, str) else 0
    top_level_type = _shape_type(raw) if json_parse_state == "parsed" else "unavailable"
    source = raw if isinstance(raw, dict) and json_parse_state == "parsed" else {}
    allowed_fields = tuple(
        (name, name in source, _shape_type(source[name]) if name in source else "unavailable")
        for name in FINAL_SHAPE_FIELD_NAMES
    )
    unknown_field_count = sum(1 for name in source if name not in FINAL_SHAPE_FIELD_NAMES)
    return FinalShapeDiagnostic(
        finish_reason="stop",
        content_length=content_length,
        json_parse_state=json_parse_state,
        top_level_type=top_level_type,
        allowed_fields=allowed_fields,
        unknown_field_count=unknown_field_count,
    )


@dataclass(frozen=True)
class ProviderDiagnostic:
    """Provider 失败使用的固定形状公开元数据。"""

    category: str
    retryable: bool = False
    retry_exhausted: bool = False
    http_status: int | None = None
    finish_reason: str | None = None
    agent_attempt: int | None = None
    model_step: int | None = None
    final_shape: FinalShapeDiagnostic | None = None

    def __post_init__(self) -> None:
        if self.category not in DIAGNOSTIC_CATEGORIES:
            raise ValueError("provider diagnostic category is not allowed")
        if self.retryable and self.category not in TRANSIENT_DIAGNOSTIC_CATEGORIES:
            raise ValueError("only transient provider diagnostics may be retryable")
        if self.retry_exhausted and not self.retryable:
            raise ValueError("retry exhaustion requires a retryable diagnostic")
        if self.http_status is not None and not 100 <= self.http_status <= 599:
            raise ValueError("provider diagnostic HTTP status is invalid")
        if self.finish_reason is not None and self.finish_reason not in {
            "length", "content_filter", "insufficient_system_resource", "stop", "tool_calls",
        }:
            raise ValueError("provider diagnostic finish reason is not allowed")
        if self.agent_attempt is not None and self.agent_attempt < 1:
            raise ValueError("provider diagnostic attempt is invalid")
        if self.model_step is not None and self.model_step < 1:
            raise ValueError("provider diagnostic model step is invalid")
        if self.final_shape is not None and self.finish_reason != "stop":
            raise ValueError("final shape diagnostics require finish_reason=stop")

    def with_context(self, *, agent_attempt: int | None = None, model_step: int | None = None) -> "ProviderDiagnostic":
        return ProviderDiagnostic(
            category=self.category,
            retryable=self.retryable,
            retry_exhausted=self.retry_exhausted,
            http_status=self.http_status,
            finish_reason=self.finish_reason,
            agent_attempt=agent_attempt if agent_attempt is not None else self.agent_attempt,
            model_step=model_step if model_step is not None else self.model_step,
            final_shape=self.final_shape,
        )

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "category": self.category,
            "http_status": self.http_status,
            "retryable": self.retryable,
            "retry_exhausted": self.retry_exhausted,
            "finish_reason": self.finish_reason,
            "agent_attempt": self.agent_attempt,
            "model_step": self.model_step,
        }
        if self.final_shape is not None:
            result["final_shape"] = self.final_shape.as_dict()
        return result

    def public_text(self) -> str:
        fields = [f"category={self.category}"]
        for key, value in self.as_dict().items():
            if key not in {"category", "final_shape"} and value is not None:
                fields.append(f"{key}={str(value).lower() if isinstance(value, bool) else value}")
        return "provider diagnostic: " + ", ".join(fields)


class TrustedDiagnosticError(AgentRuntimeError):
    """公开详情被严格限制为 ProviderDiagnostic 的错误。"""

    def __init__(self, diagnostic: ProviderDiagnostic, *, requires_human: bool = False) -> None:
        self.diagnostic = diagnostic
        self.retryable_request = diagnostic.retryable and not diagnostic.retry_exhausted
        self.requires_human = requires_human
        super().__init__(diagnostic.public_text())


