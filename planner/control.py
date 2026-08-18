"""Planner 业务控制面的兼容空壳。

旧 ``preflight-*`` 命令名暂时保留，避免其他 ``loopctl`` 子命令因导入失败而不可用。
Planner 重新实现前，所有业务入口都会明确拒绝执行，且不会访问数据库。
"""

from __future__ import annotations

import argparse

from loopdb import LoopError


PLANNER_UNAVAILABLE = "Planner 业务尚未实现；当前仅运行 heartbeat 服务"


def _reject_business_operation(args: argparse.Namespace) -> None:
    """拒绝仍在调用旧 Planner 协议的客户端。"""
    del args
    raise LoopError(PLANNER_UNAVAILABLE)


command_preflight_claim = _reject_business_operation
command_preflight_heartbeat = _reject_business_operation
command_preflight_ready = _reject_business_operation
command_preflight_needs_review = _reject_business_operation
command_preflight_fail = _reject_business_operation
