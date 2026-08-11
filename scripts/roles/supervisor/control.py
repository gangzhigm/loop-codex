"""Supervisor 的统一命令入口。

Supervisor 负责本机 Dashboard 的服务生命周期，而不是任务调度或业务执行。本文件把
外部调用收口为两个显式命令：``health`` 运行一次健康检查并在必要时恢复 Dashboard，
``serve`` 前台运行 Dashboard HTTP 服务。具体探活、进程恢复、HTTP 路由和 Secret API
仍分别位于 ``health_run.py`` 与 ``dashboard_server.py``，避免入口层复制安全逻辑。

Windows 计划任务应调用 ``health``；人工临时排障才使用 ``serve``。两个命令都把数据库
和初始化配置路径原样传给下层实现，入口本身不读取或写入 SQLite，也不修改任务状态。
"""

from __future__ import annotations

# 中文排查：计划任务的统一入口是 health；服务前台调试入口是 serve。
# 本文件只转发参数，不负责进程管理、HTTP 安全或数据库写入。

import argparse
import sys
from pathlib import Path
from typing import Sequence

SCRIPTS_ROOT = Path(__file__).resolve().parents[2]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from loopdb import CONFIG_PATH, DEFAULT_DB
from roles.supervisor import dashboard_server, health_run


def parser() -> argparse.ArgumentParser:
    """构建 Supervisor 命令行，并要求调用方明确选择健康检查或前台服务。"""
    root = argparse.ArgumentParser(description="Local Agent Loop Supervisor entry point")
    commands = root.add_subparsers(dest="command", required=True)

    health = commands.add_parser("health", help="运行一次 Dashboard 健康检查与必要恢复")
    health.add_argument("--db", default=str(DEFAULT_DB))
    health.add_argument("--config", default=str(CONFIG_PATH))

    serve = commands.add_parser("serve", help="前台运行本机 Dashboard Server")
    serve.add_argument("--db", default=str(DEFAULT_DB))
    serve.add_argument("--config", default=str(CONFIG_PATH))
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    return root


def command_arguments(args: argparse.Namespace) -> list[str]:
    """将入口层解析结果还原为下层脚本稳定接受的参数列表。

    这里不重新解析配置，也不改变默认值。``serve`` 仅在调用方明确提供 host 或 port
    时才转发覆盖参数，从而保持初始化配置仍是部署参数的唯一事实源。
    """
    values = ["--db", str(args.db), "--config", str(args.config)]
    if args.command == "serve":
        if args.host is not None:
            values.extend(["--host", str(args.host)])
        if args.port is not None:
            values.extend(["--port", str(args.port)])
    return values


def main(argv: Sequence[str] | None = None) -> None:
    """分发一次 Supervisor 命令，不吞掉下层的退出码或异常。

    ``health`` 会自行输出 JSON 并通过 ``SystemExit`` 返回健康状态；``serve`` 会一直
    运行到收到终止信号。入口保持异常可见，便于计划任务和手工终端获得真实失败原因。
    """
    args = parser().parse_args(argv)
    values = command_arguments(args)
    if args.command == "health":
        health_run.main(values)
        return
    dashboard_server.main(values)


if __name__ == "__main__":
    main()
