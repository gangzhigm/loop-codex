"""跨 Runner 使用的公开诊断脱敏。"""

from __future__ import annotations

import re


SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|authorization)\s*[:=]\s*\S+"),
    re.compile(
        r"(?i)(?:[A-Za-z]:\\|/)[^\r\n\"']*[\\/](?:\.codex|\$CODEX_HOME)(?:[\\/][^\r\n\"']*)?"
    ),
)


def sanitize_public_text(value: str, maximum: int = 1000) -> str:
    """移除认证材料、私有工具目录和控制字符后截断公开文本。"""
    text = value.replace("\x00", " ")
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    text = re.sub(r"[\r\n]+", " ", text).strip()
    return text[:maximum]
