"""Runtime 核心类型、路由解析、安全环境和事件日志。"""

from __future__ import annotations

# 中文排查：Runtime 的配置快照、公共异常、脱敏日志和子进程环境清理位于这里。
# 路由不匹配先核对 runtime_environment/provider/capability 三元组和领取时配置快照。
# 子进程环境必须移除敏感变量；调试时不得通过继承完整父进程环境来绕过问题。

import json
import sys
from dataclasses import dataclass
from typing import Any, Protocol

from common.processes import safe_process_environment
from loop_agent.runtime.diagnostics import AgentRuntimeError
from loopdb import resolve_execution_profile


def safe_subprocess_environment() -> dict[str, str]:
    """复制进程环境，但不传播注入的凭据。

    Provider 通过 ``SecretStore`` 获取凭据。即使父 Agent 进程调用 Provider 时需要某个凭据，
    子工具也不会继承名称表明其包含密钥的环境变量。
    """
    return safe_process_environment()


class ToolRejected(AgentRuntimeError):
    """请求的工具调用违反了本地沙箱契约。"""


class ApprovalRequired(AgentRuntimeError):
    """请求了尚未获得任务级批准的高风险操作。"""

    def __init__(self, action: str) -> None:
        self.action = action
        super().__init__(f"action requires explicit approval: {action}")


class AgentAttemptTimeout(AgentRuntimeError):
    """完整 Agent attempt 超过了对应路由的截止时间。"""


class ModelRequestTimeout(AgentRuntimeError):
    """单次模型请求超过了配置的请求超时。"""


class OwnedWorkStillRunning(AgentRuntimeError):
    """超时的 Provider 线程可能仍持有副作用或资源。"""


class ModelProvider(Protocol):
    """由 DeepSeek 及后续适配器实现的中立 Provider 边界。"""

    def complete(
        self, request: dict[str, Any], timeout_seconds: float
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ExecutionProfile:
    """单个已领取 execution 使用的不可变路由快照。"""

    runtime_environment: str
    provider_id: str | None
    capability_level: str
    model: str
    reasoning: str
    attempt_timeout_seconds: float
    max_retries: int

    @classmethod
    def resolve(
        cls,
        config: dict[str, Any],
        runtime_environment: str,
        provider_id: str | None,
        capability_level: str,
    ) -> "ExecutionProfile":
        try:
            value = resolve_execution_profile(
                runtime_environment, provider_id, capability_level, config
            )
        except Exception as error:
            raise AgentRuntimeError(
                "no unique execution profile matches the requested route"
            ) from error
        return cls(
            runtime_environment=str(value["runtime_environment"]),
            provider_id=value["provider_id"],
            capability_level=str(value["capability_level"]),
            model=str(value["model"]),
            reasoning=str(value["reasoning"]),
            attempt_timeout_seconds=float(value["attempt_timeout_seconds"]),
            max_retries=int(value["max_retries"]),
        )

    def request_payload(self) -> dict[str, Any]:
        """只返回允许 Provider 查看的一组路由字段。"""
        return {
            "runtime_environment": self.runtime_environment,
            "provider_id": self.provider_id,
            "capability_level": self.capability_level,
            "model": self.model,
            "reasoning": self.reasoning,
        }


@dataclass(frozen=True)
class RuntimeSettings:
    """单个 Runtime 进程共享的已校验运行限制。"""

    max_steps: int
    model_timeout_seconds: float
    tool_timeout_seconds: float
    heartbeat_interval_seconds: float
    max_file_bytes: int
    max_tool_output_chars: int
    stalled_after_seconds: float = 300
    provider_termination_grace_seconds: float = 5
    max_final_repairs: int = 1

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "RuntimeSettings":
        agent = config["self_hosted_agent"]
        settings = cls(
            max_steps=int(agent["max_steps"]),
            model_timeout_seconds=float(agent["model_timeout_seconds"]),
            tool_timeout_seconds=float(agent["tool_timeout_seconds"]),
            heartbeat_interval_seconds=float(
                config["task_execution"]["heartbeat_interval_seconds"]
            ),
            max_file_bytes=int(agent["max_file_bytes"]),
            max_tool_output_chars=int(agent["max_tool_output_chars"]),
            stalled_after_seconds=float(
                config["task_execution"]["stalled_after_seconds"]
            ),
            provider_termination_grace_seconds=float(
                agent["provider_termination_grace_seconds"]
            ),
            max_final_repairs=int(agent.get("max_final_repairs", 1)),
        )
        if settings.max_final_repairs not in {0, 1}:
            raise AgentRuntimeError("max_final_repairs must be zero or one")
        if not (
            0
            < settings.heartbeat_interval_seconds
            < settings.stalled_after_seconds
        ):
            raise AgentRuntimeError("heartbeat interval must be below stalled detection")
        profiles = config.get("execution_profiles") or {}
        attempts = [
            profile["attempt_timeout_seconds"]
            for runtime in profiles.values()
            for provider in (
                list((runtime.get("providers") or {}).values())
                if isinstance(runtime, dict) and "providers" in runtime
                else [runtime]
            )
            for profile in (provider.get("capabilities") or {}).values()
        ]
        if not attempts or any(
            float(timeout) <= settings.stalled_after_seconds for timeout in attempts
        ):
            raise AgentRuntimeError(
                "stalled detection must be below every attempt timeout"
            )
        return settings


class SafeLogger:
    """只记录事件元数据，绝不记录提示词、推理或文件内容。"""

    def __init__(self, stream: Any = None) -> None:
        self.stream = stream or sys.stderr

    def event(self, name: str, **fields: Any) -> None:
        safe = {
            key: str(value)[:160]
            for key, value in fields.items()
            if key not in {"content", "prompt", "authorization"}
        }
        print(
            json.dumps({"event": name, **safe}, ensure_ascii=False),
            file=self.stream,
            flush=True,
        )
