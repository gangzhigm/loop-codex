"""受限的 Runner 验证 Worker 配置与任务标记判断。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loopdb import BASE_DIR


@dataclass(frozen=True)
class ValidationWorkerSettings:
    """验证 Worker 只能处理带精确标记的 Planner 测试任务。"""

    enabled: bool
    task_id_prefix: str
    task_marker: str
    planner_ready_delay_seconds: int
    artifact_directory: Path

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "ValidationWorkerSettings":
        runner = config.get("runner")
        raw = runner.get("validation_worker") if isinstance(runner, dict) else None
        if not isinstance(raw, dict):
            raise ValueError("runner.validation_worker 配置缺失")
        enabled = raw.get("enabled")
        prefix = raw.get("task_id_prefix")
        marker = raw.get("task_marker")
        delay = raw.get("planner_ready_delay_seconds")
        directory = raw.get("artifact_directory")
        if (
            not isinstance(enabled, bool)
            or not isinstance(prefix, str)
            or not prefix.strip()
            or not isinstance(marker, str)
            or not marker.strip()
            or not isinstance(delay, int)
            or delay < 1
            or not isinstance(directory, str)
            or not directory.strip()
        ):
            raise ValueError("runner.validation_worker 配置无效")
        artifact_directory = (BASE_DIR / directory).resolve()
        if not artifact_directory.is_relative_to(BASE_DIR):
            raise ValueError("runner.validation_worker.artifact_directory 不安全")
        return cls(enabled, prefix.strip(), marker.strip(), delay, artifact_directory)


def is_validation_task(task_id: object, description: object, settings: ValidationWorkerSettings) -> bool:
    """要求任务 ID 前缀和描述中的单独标记行同时匹配。"""
    return (
        isinstance(task_id, str)
        and task_id.startswith(settings.task_id_prefix)
        and isinstance(description, str)
        and any(line.strip() == settings.task_marker for line in description.splitlines())
    )
