"""Self-hosted Agent 入口共用的 Provider 装配和有界调用。"""

from __future__ import annotations

import importlib
import inspect
import queue
import threading
from typing import Any

from loop_agent.runtime.core import (
    AgentAttemptTimeout,
    ModelProvider,
    ModelRequestTimeout,
    OwnedWorkStillRunning,
)
from loop_agent.runtime.diagnostics import AgentRuntimeError
from loop_agent.secrets.store import SecretStore, create_secret_store


def provider_factory(config: dict[str, Any], provider_id: str) -> str:
    """读取 Provider 工厂映射，拒绝 Dispatcher 或 Planner 自行拼接模块路径。"""
    factories = (config.get("self_hosted_agent") or {}).get("provider_factories") or {}
    specification = factories.get(provider_id)
    if not isinstance(specification, str) or not specification.strip():
        raise AgentRuntimeError("provider factory is not configured")
    return specification


def load_provider(
    specification: str,
    config: dict[str, Any],
    secret_store: SecretStore | None = None,
) -> ModelProvider:
    """加载 Provider 工厂，并显式注入受控 SecretStore。"""
    if ":" not in specification:
        raise AgentRuntimeError("provider must use module:factory syntax")
    module_name, attribute = specification.split(":", 1)
    factory = getattr(importlib.import_module(module_name), attribute)
    store = secret_store or create_secret_store(config)
    try:
        inspect.signature(factory).bind(config=config, secret_store=store)
    except (TypeError, ValueError):
        raise AgentRuntimeError(
            "provider factory must accept config and secret_store keyword arguments"
        ) from None
    provider = factory(config=config, secret_store=store)
    if not callable(getattr(provider, "complete", None)):
        raise AgentRuntimeError("provider factory did not return a ModelProvider")
    return provider


def call_provider(
    provider: ModelProvider,
    request: dict[str, Any],
    request_timeout_seconds: float,
    wait_timeout_seconds: float,
    termination_grace_seconds: float,
) -> dict[str, Any]:
    """在硬截止时间内调用 Provider，避免网络实现无限阻塞 Runner。"""
    completed: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            completed.put(
                ("result", provider.complete(request, request_timeout_seconds))
            )
        except BaseException as error:
            completed.put(("error", error))

    thread = threading.Thread(target=invoke, name="model-provider", daemon=True)
    thread.start()
    try:
        outcome, value = completed.get(timeout=wait_timeout_seconds)
    except queue.Empty as error:
        thread.join(timeout=termination_grace_seconds)
        if thread.is_alive():
            raise OwnedWorkStillRunning(
                "model provider remained active after the request timeout"
            ) from error
        raise AgentAttemptTimeout("agent attempt timed out") from error
    if outcome == "error":
        if isinstance(value, TimeoutError):
            raise ModelRequestTimeout("model provider request timed out") from value
        raise value
    if not isinstance(value, dict):
        raise AgentRuntimeError("model provider returned a non-object")
    return value
