"""Supervisor 的根目录主入口。

``serve`` 是长期运行的 Supervisor 进程入口：当前阶段负责承载本机 Dashboard 服务，
后续 Supervisor 的调度、观察和恢复能力也应从这里扩展。``health`` 只执行一次外部
健康检查；它由 Windows 计划任务周期调用，发现主进程不健康时会在后台重新启动本文件的
``serve`` 命令。

入口层不直接读写任务状态。HTTP 服务与一次性健康检查仍分别复用既有的稳定实现，避免
在迁移入口时复制安全校验、进程锁或恢复逻辑。
"""

from __future__ import annotations

# 中文排查：计划任务调用 health；由 health 恢复的常驻主进程调用 serve。
# 本文件只组织 Supervisor 命令边界，任务状态写入仍只能经过 scripts/loopctl.py。

import argparse
import sys
from pathlib import Path
from typing import Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from loopdb import CONFIG_PATH, DEFAULT_DB
if __package__:
    from . import health_run
    from client import dashboard_server
else:
    # 计划任务以 ``python supervisor/main.py`` 直接执行时，没有包上下文。
    import health_run
    from client import dashboard_server


def parser() -> argparse.ArgumentParser:
    """创建 Supervisor 命令行，明确区分短暂检查和常驻服务。"""
    value = argparse.ArgumentParser(description="Local Agent Loop Supervisor entry point")
    commands = value.add_subparsers(dest="command", required=True)

    health = commands.add_parser("health", help="检查 Supervisor，并在必要时恢复常驻进程")
    health.add_argument("--db", default=str(DEFAULT_DB))
    health.add_argument("--config", default=str(CONFIG_PATH))

    serve = commands.add_parser("serve", help="常驻运行 Supervisor 主进程")
    serve.add_argument("--db", default=str(DEFAULT_DB))
    serve.add_argument("--config", default=str(CONFIG_PATH))
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    return value


def command_arguments(args: argparse.Namespace) -> list[str]:
    """把入口参数转为下层实现使用的稳定参数列表。"""
    values = ["--db", str(args.db), "--config", str(args.config)]
    if args.command == "serve":
        if args.host is not None:
            values.extend(["--host", str(args.host)])
        if args.port is not None:
            values.extend(["--port", str(args.port)])
    return values


def main(argv: Sequence[str] | None = None) -> None:
    """执行一次命令分发；serve 持续运行，health 完成检查后立即退出。"""
    args = parser().parse_args(argv)
    values = command_arguments(args)
    if args.command == "health":
        health_run.main(values)
        return
    dashboard_server.main(values)


if __name__ == "__main__":
    main()
