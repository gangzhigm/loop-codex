"""Dashboard 与健康任务共享的服务启停期望状态。"""

from __future__ import annotations

from typing import Any

from loopdb import now_shanghai

from common.files import read_json_object, write_json_atomic
from common.paths import SERVICE_CONTROL


SERVICES = ("supervisor", "planner")


def service_control_state() -> dict[str, bool]:
    """读取人工期望状态；缺失或损坏字段按启用处理。"""
    raw = read_json_object(SERVICE_CONTROL) or {}
    return {
        service: raw.get(service) if isinstance(raw.get(service), bool) else True
        for service in SERVICES
    }


def set_service_enabled(service: str, enabled: bool) -> dict[str, bool]:
    """原子保存一个服务的人工期望状态并返回完整快照。"""
    if service not in SERVICES:
        raise ValueError("未知服务")
    state = service_control_state()
    state[service] = enabled
    SERVICE_CONTROL.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        **state,
        "updated_at": now_shanghai(),
    }
    write_json_atomic(SERVICE_CONTROL, payload)
    return state
