"""Self-hosted Worker 的单任务业务执行入口。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = REPOSITORY_ROOT / "control"
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from common.providers import load_provider
from loop_agent.runtime.agent import SingleTaskAgent
from loop_agent.runtime.controller import SubprocessLoopController
from loop_agent.runtime.core import RuntimeSettings
from loop_agent.runtime.diagnostics import AgentRuntimeError
from loopdb import (
    CAPABILITY_LEVELS,
    CANONICAL_RUNTIME_ENVIRONMENTS,
    CONFIG_PATH,
    load_initialization_config,
)


def parser() -> argparse.ArgumentParser:
    """构建 Worker 业务执行参数并限制合法路由。"""
    root = argparse.ArgumentParser(
        description="Single-run self-hosted Local Agent Worker"
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
    """领取单个任务、执行 Worker 业务并通过受控 loopctl 写回结果。"""
    args = parser().parse_args()
    config = load_initialization_config(Path(args.config))
    workspace = Path(config["workspace"]["task_root"])
    provider = load_provider(args.provider, config)
    controller = SubprocessLoopController(Path(args.db) if args.db else None)
    agent = SingleTaskAgent(
        provider=provider,
        controller=controller,
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
                    "outcome": "WORKER_ERROR",
                    "error": SingleTaskAgent._public_error(error),
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(1)
