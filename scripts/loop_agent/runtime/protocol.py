"""Validate model envelopes, tool arguments, and final task reports.

Provider adapters are untrusted at this boundary. A response must first match
the neutral envelope, then each tool call is checked for exact argument names
and types. Final results are normalized into the smaller payload accepted by
``loopctl finish``. Provider-supplied diagnostic hints never bypass these
structural checks.
"""

from __future__ import annotations

# 中文排查：模型响应、工具参数、高风险批准和最终结果 Schema 在此进行纯校验。
# 拒绝响应时先判断 envelope、tool_calls/final 二选一，再核对具体工具或终态字段。
# 本模块不执行工具和数据库写入，校验函数应保持确定性，便于用单元测试复现。

import re
from typing import Any

from loop_agent.runtime.contracts import (
    FINAL_STATUSES,
    HIGH_RISK_ACTIONS,
)
from loop_agent.runtime.core import AgentRuntimeError
from loop_agent.runtime.diagnostics import (
    FinalShapeDiagnostic,
    ProviderDiagnostic,
    TrustedDiagnosticError,
    final_shape_diagnostic,
)
from loop_agent.runtime.sandbox import ToolSandbox


def approved_actions(task: dict[str, Any]) -> set[str]:
    """Parse the explicit high-risk approval marker from task-owned text."""
    text = "\n".join(
        [
            str(task.get("description") or ""),
            *[str(item) for item in task.get("acceptance") or []],
        ]
    )
    match = re.search(
        r"APPROVED_ACTIONS\s*:\s*([a-z_, -]+)", text, re.IGNORECASE
    )
    if not match:
        return set()
    return {
        item.strip().lower()
        for item in match.group(1).split(",")
        if item.strip().lower() in HIGH_RISK_ACTIONS
    }


def validate_model_response(raw: Any) -> dict[str, Any]:
    """Validate and normalize one Provider response envelope."""
    if not isinstance(raw, dict) or raw.get("type") not in {
        "tool_calls",
        "final",
    }:
        raise AgentRuntimeError("model returned an invalid response envelope")
    if raw["type"] == "tool_calls":
        calls = raw.get("calls")
        if not isinstance(calls, list) or not calls or len(calls) > 8:
            raise AgentRuntimeError("model returned invalid tool calls")
        normalized = []
        known_tools = {
            item["name"] for item in ToolSandbox.TOOL_SCHEMAS
        } | HIGH_RISK_ACTIONS
        for call in calls:
            if (
                not isinstance(call, dict)
                or not isinstance(call.get("id"), str)
                or not call["id"]
            ):
                raise AgentRuntimeError("model returned a tool call without id")
            if not isinstance(call.get("name"), str) or not isinstance(
                call.get("arguments"), dict
            ):
                raise AgentRuntimeError("model returned an invalid tool call")
            if call["name"] not in known_tools:
                raise AgentRuntimeError("model requested an unknown tool")
            validate_tool_arguments(call["name"], call["arguments"])
            normalized.append(
                {
                    "id": call["id"],
                    "name": call["name"],
                    "arguments": call["arguments"],
                }
            )
        return {"type": "tool_calls", "calls": normalized}
    try:
        result = validate_final_result(raw.get("result"))
    except AgentRuntimeError:
        shape = raw.get("final_shape")
        if not isinstance(shape, FinalShapeDiagnostic):
            shape = final_shape_diagnostic(raw.get("result"))
        raise TrustedDiagnosticError(
            ProviderDiagnostic(
                "final_schema", finish_reason="stop", final_shape=shape
            )
        ) from None
    return {"type": "final", "result": result}


def validate_tool_arguments(name: str, arguments: dict[str, Any]) -> None:
    """Reject extra or malformed arguments before the sandbox sees a call.

    High-risk names deliberately reach ``ToolSandbox`` so its approval gate can
    produce ``WAITING_HUMAN``. There is still no implementation behind them.
    """
    if name in HIGH_RISK_ACTIONS:
        return
    schemas: dict[str, dict[str, Any]] = {
        "read_file": {"required": {"path": str}, "optional": {}},
        "search": {
            "required": {"path": str, "pattern": str},
            "optional": {},
        },
        "apply_patch": {
            "required": {"path": str, "old": str, "new": str},
            "optional": {},
        },
        "run_command": {
            "required": {"argv": list, "cwd": str},
            "optional": {},
        },
    }
    schema = schemas.get(name)
    if schema is None:
        raise AgentRuntimeError("model requested an unknown tool")
    allowed = set(schema["required"]) | set(schema["optional"])
    if set(arguments) - allowed:
        raise AgentRuntimeError("model tool call contains unknown arguments")
    for field, expected in schema["required"].items():
        if field not in arguments or not isinstance(arguments[field], expected):
            raise AgentRuntimeError("model tool call has invalid arguments")
    if name == "run_command" and (
        not arguments["argv"]
        or not all(
            isinstance(item, str) and item for item in arguments["argv"]
        )
    ):
        raise AgentRuntimeError("model tool call has invalid arguments")


def validate_final_result(raw: Any) -> dict[str, Any]:
    """Normalize a final model result into the control-plane report contract."""
    if not isinstance(raw, dict) or raw.get("status") not in FINAL_STATUSES:
        raise AgentRuntimeError("final result status is invalid")
    status = raw["status"]
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise AgentRuntimeError("final result summary is required")
    result: dict[str, Any] = {
        "status": status,
        "summary": summary.strip()[:4000],
    }
    verification = raw.get("verification") or []
    if not isinstance(verification, list) or not all(
        isinstance(item, str) and item.strip() for item in verification
    ):
        raise AgentRuntimeError("verification must be a string array")
    if status == "SUCCEEDED" and not verification:
        raise AgentRuntimeError("SUCCEEDED requires verification")
    if verification:
        result["verification"] = [
            item.strip()[:1000] for item in verification[:50]
        ]
    completed = raw.get("completed") or []
    if completed:
        if not isinstance(completed, list) or not all(
            isinstance(item, str) for item in completed
        ):
            raise AgentRuntimeError("completed must be a string array")
        result["completed"] = [
            item.strip()[:1000] for item in completed[:50] if item.strip()
        ]
    if status == "FAILED":
        error = raw.get("error")
        if not isinstance(error, str) or not error.strip():
            raise AgentRuntimeError("FAILED requires error")
        result["error"] = error.strip()[:4000]
    if status == "WAITING_HUMAN":
        question = raw.get("question")
        if not isinstance(question, str) or not question.strip():
            raise AgentRuntimeError("WAITING_HUMAN requires question")
        result["question"] = question.strip()[:4000]
        options = raw.get("options") or []
        if not isinstance(options, list) or not all(
            isinstance(item, str) for item in options
        ):
            raise AgentRuntimeError("options must be a string array")
        result["options"] = [
            item.strip()[:500] for item in options[:20] if item.strip()
        ]
        result["next_step"] = str(
            raw.get("next_step") or "等待人工答复。"
        ).strip()[:1000]
        result["percent"] = max(0, min(99, int(raw.get("percent", 0))))
    return result
