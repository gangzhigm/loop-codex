"""Core runtime types, route resolution, safe environment, and event logging."""

from __future__ import annotations

# 中文排查：Runtime 的配置快照、公共异常、脱敏日志和子进程环境清理位于这里。
# 路由不匹配先核对 runtime_environment/provider/capability 三元组和领取时配置快照。
# 子进程环境必须移除敏感变量；调试时不得通过继承完整父进程环境来绕过问题。

import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Protocol

from loop_agent.runtime.contracts import SENSITIVE_ENVIRONMENT_NAME
from loop_agent.runtime.diagnostics import AgentRuntimeError
from loopdb import resolve_execution_profile


def safe_subprocess_environment() -> dict[str, str]:
    """Copy process settings without propagating injected credentials.

    Providers receive credentials through ``SecretStore``. Child tools do not
    inherit variables whose names indicate secrets, even when the parent Agent
    process needed those values to call its provider.
    """
    return {
        name: value
        for name, value in os.environ.items()
        if not SENSITIVE_ENVIRONMENT_NAME.search(name)
    }


class ToolRejected(AgentRuntimeError):
    """A requested tool call violated the local sandbox contract."""


class ApprovalRequired(AgentRuntimeError):
    """A high-risk action was requested without task-level approval."""

    def __init__(self, action: str) -> None:
        self.action = action
        super().__init__(f"action requires explicit approval: {action}")


class AgentAttemptTimeout(AgentRuntimeError):
    """The complete Agent attempt exceeded its route-specific deadline."""


class ModelRequestTimeout(AgentRuntimeError):
    """One model request exceeded the configured per-request timeout."""


class OwnedWorkStillRunning(AgentRuntimeError):
    """A timed-out provider thread may still own side effects or resources."""


class ModelProvider(Protocol):
    """Neutral provider boundary implemented by DeepSeek and future adapters."""

    def complete(
        self, request: dict[str, Any], timeout_seconds: float
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ExecutionProfile:
    """Immutable route snapshot used for one claimed execution."""

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
        """Return only route fields that a provider is allowed to inspect."""
        return {
            "runtime_environment": self.runtime_environment,
            "provider_id": self.provider_id,
            "capability_level": self.capability_level,
            "model": self.model,
            "reasoning": self.reasoning,
        }


@dataclass(frozen=True)
class RuntimeSettings:
    """Validated operational limits shared by one runtime process."""

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
    """Log event metadata only, never prompts, reasoning, or file contents."""

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
