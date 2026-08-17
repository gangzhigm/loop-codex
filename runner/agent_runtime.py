"""Self-hosted 单任务 Agent Runtime 的启动入口。

真正实现位于 ``loop_agent.runtime``，本文件只负责解析启动参数、装配 Provider 和运行
单个 Agent execution。目录职责如下：

* ``contracts``：Provider 协议、敏感名称模式和高风险动作清单；
* ``core``：运行设置、路由快照、异常、安全日志与子进程环境；
* ``controller``：loopctl 子进程适配器和后台心跳；
* ``sandbox``：scope 路径策略和本地工具实现；
* ``protocol``：模型响应、工具参数及最终报告校验；
* ``diagnostics``：可信且可公开的 Provider/最终结果结构诊断；
* ``agent``：一次领取、一次完成的主编排循环。

部署入口保持为 ``python runner/agent_runtime.py ...``。Provider 工厂继续
使用 ``module:factory`` 约定，必须显式接收 config 和 SecretStore。
"""

from __future__ import annotations

# 中文排查：这是 Self-hosted Agent 的启动入口，具体工具循环位于 loop_agent/runtime。
# 启动失败先检查运行环境、Provider 工厂签名和 SecretStore；执行失败再进入 runtime/agent.py。

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
from common.providers import load_provider
from common.runners import ObservedWorkerController, RunnerState


def parser() -> argparse.ArgumentParser:
    """构建单次 Runtime 命令行，并限制合法环境、等级和策略。"""
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
    """装配 Provider、loopctl 控制器和单任务 Agent，执行指定 execution 后输出 JSON。"""
    args = parser().parse_args()
    config = load_initialization_config(Path(args.config))
    workspace = Path(config["workspace"]["task_root"])
    provider = load_provider(args.provider, config)
    state = RunnerState(
        args.execution_id,
        "worker",
        args.runtime_environment,
        args.execution_id,
    )
    state.start()
    try:
        controller = ObservedWorkerController(
            SubprocessLoopController(Path(args.db) if args.db else None), state
        )
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
    finally:
        state.close()


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
