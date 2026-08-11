"""Stable provider/tool protocol constants for the self-hosted Agent runtime.

Keeping schemas and safety patterns in one small module makes protocol reviews
possible without reading the orchestration loop. These values are code-owned
runtime contracts, not deployment configuration.
"""

from __future__ import annotations

# 中文排查：Provider envelope、终态 JSON、工具名称和敏感环境变量等静态契约集中在这里。
# 协议校验突然失败时先比较版本、必填字段和允许列表，不要先修改解析器。
# 修改常量会影响 Provider、Runtime 和测试，必须同步检查三处使用方。

import re


FINAL_STATUSES = {"SUCCEEDED", "FAILED", "WAITING_HUMAN"}
FINAL_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["SUCCEEDED", "FAILED", "WAITING_HUMAN"],
        },
        "summary": {"type": "string", "minLength": 1},
        "verification": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "completed": {"type": "array", "items": {"type": "string"}},
        "error": {"type": "string", "minLength": 1},
        "question": {"type": "string", "minLength": 1},
        "options": {"type": "array", "items": {"type": "string"}},
        "next_step": {"type": "string"},
        "percent": {"type": "integer", "minimum": 0, "maximum": 99},
    },
    "required_by_status": {
        "SUCCEEDED": ["status", "summary", "verification"],
        "FAILED": ["status", "summary", "error"],
        "WAITING_HUMAN": ["status", "summary", "question"],
    },
    "examples": {
        "SUCCEEDED": {
            "status": "SUCCEEDED",
            "summary": "Work completed.",
            "verification": ["A concrete verification passed."],
        },
        "FAILED": {
            "status": "FAILED",
            "summary": "Work failed.",
            "error": "Failure category.",
        },
        "WAITING_HUMAN": {
            "status": "WAITING_HUMAN",
            "summary": "Human input is required.",
            "question": "What decision is required?",
        },
    },
}

# These action names require an explicit task-level approval even if a future
# sandbox grows an implementation for them.
HIGH_RISK_ACTIONS = {
    "delete",
    "publish",
    "git_commit",
    "external_message",
    "credential_access",
}

# Component checks apply to paths, while environment checks prevent injected
# secrets from being inherited by git, tests, and other child processes.
SENSITIVE_COMPONENT = re.compile(
    r"(^|[._-])(secret|secrets|credential|credentials|api[_-]?key|access[_-]?token|private[_-]?key)([._-]|$)",
    re.IGNORECASE,
)
SENSITIVE_ENVIRONMENT_NAME = re.compile(
    r"(secret|credential|password|api[_-]?key|access[_-]?token|private[_-]?key|authorization)",
    re.IGNORECASE,
)
SHELL_META = re.compile(r"[|&;<>`\r\n]")
