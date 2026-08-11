"""Read task execution limits from initialization configuration."""

from __future__ import annotations

# 中文排查：并发上限、平台容量和 execution 时间参数统一从初始化配置读取。
# 容量结果异常时同时核对全局值、平台值和运行环境名称，避免只看单一上限。
# 这些部署参数不属于 SQLite 任务事实，禁止为了查询方便复制进数据库。

from typing import Any

from loop_agent.configuration import load_initialization_config
from loop_agent.constants import CANONICAL_RUNTIME_ENVIRONMENTS
from loop_agent.errors import LoopError


def execution_setting(
    key: str,
    default: Any = None,
    config: dict[str, Any] | None = None,
) -> Any:
    value = config or load_initialization_config()
    return value.get("task_execution", {}).get(key, default)


def global_parallel_limit(config: dict[str, Any] | None = None) -> int:
    return int(execution_setting("global_max_active_executions", 8, config))


def platform_parallel_limit(
    platform: str, config: dict[str, Any] | None = None
) -> int:
    if platform not in CANONICAL_RUNTIME_ENVIRONMENTS:
        raise LoopError(f"执行平台无效: {platform}")
    limits = execution_setting("platform_max_active_executions", {}, config)
    return int(limits[platform])
