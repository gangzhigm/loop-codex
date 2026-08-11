"""Compatibility entry point for the self-hosted single-task Agent runtime.

The implementation is organized under ``loop_agent.runtime`` so each safety
boundary can be inspected independently. Existing integrations may continue to
import public names from this file; it intentionally re-exports the former API.

Module map:

* ``contracts``: provider schemas, sensitive-name patterns, high-risk actions.
* ``core``: settings, route snapshot, errors, safe logging and child env.
* ``controller``: loopctl subprocess adapter and background heartbeat.
* ``sandbox``: scope path policy and local tool implementations.
* ``protocol``: model response, tool argument, and final report validation.
* ``diagnostics``: trusted, public Provider/final-shape diagnostics.
* ``agent``: the one-claim/one-finish orchestration loop.

Deployment starts ``python scripts/roles/runner/agent_runtime.py ...``. Provider
factories continue to use the same ``module:factory`` contract.
"""

from __future__ import annotations

# 中文排查：这是 Self-hosted Agent 的稳定入口和兼容导出层，具体工具循环位于 loop_agent/runtime。
# 启动失败先检查运行环境、Provider 工厂签名和 SecretStore；执行失败再进入 runtime/agent.py。
# 本文件保留部分导出供测试和外部调用，移动名称前必须先检索所有兼容引用。

import argparse
import importlib
import inspect
import json
import subprocess  # Re-exported for existing diagnostics/tests that patch subprocess.run.
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

SCRIPTS_ROOT = Path(__file__).resolve().parents[2]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from loop_agent.runtime.agent import SingleTaskAgent
from loop_agent.runtime.contracts import (
    FINAL_RESULT_SCHEMA,
    FINAL_STATUSES,
    HIGH_RISK_ACTIONS,
    SENSITIVE_COMPONENT,
    SENSITIVE_ENVIRONMENT_NAME,
    SHELL_META,
)
from loop_agent.runtime.controller import (
    BASE_DIR,
    LOOPCTL,
    HeartbeatGuard,
    SubprocessLoopController,
)
from loop_agent.runtime.core import (
    AgentAttemptTimeout,
    ApprovalRequired,
    ExecutionProfile,
    ModelProvider,
    ModelRequestTimeout,
    OwnedWorkStillRunning,
    RuntimeSettings,
    SafeLogger,
    ToolRejected,
    safe_subprocess_environment,
)
from loop_agent.runtime.diagnostics import (
    AgentRuntimeError,
    DIAGNOSTIC_CATEGORIES,
    FINAL_SHAPE_FIELD_NAMES,
    FINAL_SHAPE_PARSE_STATES,
    FINAL_SHAPE_TYPE_TAGS,
    TRANSIENT_DIAGNOSTIC_CATEGORIES,
    FinalShapeDiagnostic,
    ProviderDiagnostic,
    TrustedDiagnosticError,
    final_shape_diagnostic,
)
from loop_agent.runtime.protocol import (
    approved_actions,
    validate_final_result,
    validate_model_response,
    validate_tool_arguments,
)
from loop_agent.runtime.sandbox import ScopePolicy, ToolSandbox
from loopdb import (
    CAPABILITY_LEVELS,
    CANONICAL_RUNTIME_ENVIRONMENTS,
    CONFIG_PATH,
    load_initialization_config,
)
from loop_agent.secrets.store import SecretStore, create_secret_store


def load_provider(
    specification: str,
    config: dict[str, Any],
    secret_store: SecretStore | None = None,
) -> ModelProvider:
    """Load a Provider factory with an explicit SecretStore dependency.

    Factories must accept exactly the named ``config`` and ``secret_store``
    inputs. This prevents adapters from silently reading ambient credentials or
    depending on the command-line wrapper's internal objects.
    """
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


def parser() -> argparse.ArgumentParser:
    """Build the unchanged single-run runtime command line."""
    root = argparse.ArgumentParser(
        description="Single-run self-hosted Local Agent Loop runtime"
    )
    root.add_argument(
        "--runtime-environment",
        required=True,
        choices=CANONICAL_RUNTIME_ENVIRONMENTS,
    )
    root.add_argument("--provider-id", required=True)
    root.add_argument(
        "--capability-level", required=True, choices=CAPABILITY_LEVELS
    )
    root.add_argument("--execution-policy", required=True, choices=("automatic",))
    root.add_argument(
        "--provider",
        required=True,
        help="Python module:factory returning a ModelProvider",
    )
    root.add_argument("--execution-id", required=True)
    root.add_argument("--config", default=str(CONFIG_PATH))
    root.add_argument("--db")
    return root


def main() -> None:
    args = parser().parse_args()
    config = load_initialization_config(Path(args.config))
    workspace = Path(config["workspace"]["task_root"])
    provider = load_provider(args.provider, config)
    agent = SingleTaskAgent(
        provider=provider,
        controller=SubprocessLoopController(Path(args.db) if args.db else None),
        workspace=workspace,
        settings=RuntimeSettings.from_config(config),
        config=config,
    )
    result = agent.run(
        args.execution_id,
        args.runtime_environment,
        args.capability_level,
        args.provider_id,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            json.dumps(
                {
                    "outcome": "RUNTIME_ERROR",
                    "error": SingleTaskAgent._public_error(error),
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(1)
