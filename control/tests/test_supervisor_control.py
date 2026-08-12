"""Supervisor 统一入口的命令分发回归测试。"""

from __future__ import annotations

# 中文排查：本测试只验证入口选项和参数透传，两个下层入口都必须 mock。
# 这里不能启动真实 Dashboard、写 PID、触发健康恢复或修改 Windows 计划任务。

import unittest
from unittest.mock import patch

from _bootstrap import REPOSITORY_ROOT

from supervisor import main as control


class SupervisorControlTests(unittest.TestCase):
    """验证 Supervisor 入口不会混淆 health 与 serve 的职责。"""

    def test_health_arguments_only_forward_database_and_config(self) -> None:
        """health 不接收 HTTP 覆盖参数，并将数据库和配置原样传给健康实现。"""
        args = control.parser().parse_args(
            ["health", "--db", "test.sqlite3", "--config", "test-config.json"]
        )
        self.assertEqual(
            control.command_arguments(args),
            ["--db", "test.sqlite3", "--config", "test-config.json"],
        )

    def test_serve_arguments_forward_explicit_host_and_port(self) -> None:
        """serve 只转发调用方明确提供的 host/port 覆盖值。"""
        args = control.parser().parse_args(
            [
                "serve",
                "--db",
                "test.sqlite3",
                "--config",
                "test-config.json",
                "--host",
                "127.0.0.1",
                "--port",
                "4999",
            ]
        )
        self.assertEqual(
            control.command_arguments(args),
            [
                "--db",
                "test.sqlite3",
                "--config",
                "test-config.json",
                "--host",
                "127.0.0.1",
                "--port",
                "4999",
            ],
        )

    def test_main_dispatches_health_without_starting_server(self) -> None:
        """health 命令只能调用 health_run.main，不能启动 Dashboard HTTP 服务。"""
        with (
            patch.object(control.health_run, "main") as health,
            patch.object(control.dashboard_server, "main") as serve,
        ):
            control.main(["health", "--db", "test.sqlite3", "--config", "test-config.json"])
        health.assert_called_once_with(
            ["--db", "test.sqlite3", "--config", "test-config.json"]
        )
        serve.assert_not_called()

    def test_main_dispatches_serve_without_running_health_check(self) -> None:
        """serve 进入常驻 Supervisor，不能触发一次性健康检查。"""
        with (
            patch.object(control.health_run, "main") as health,
            patch.object(control, "serve_supervisor") as serve,
        ):
            control.main(["serve", "--port", "4999"])
        health.assert_not_called()
        serve.assert_called_once()
        self.assertEqual(serve.call_args.args[0].port, 4999)

    def test_component_snapshot_reports_pending_planner_and_undeployed_dispatcher(self) -> None:
        """组件快照必须区分待预检任务与尚未部署的 Dispatcher。"""
        config = {
            "automations": {"planner": {"scheduled": True}},
            "codex_cli": {"dispatcher": {"task_name": "Loop Dispatcher"}},
        }
        with (
            patch.object(control, "_monitored_count", side_effect=[2, 0, 1]),
            patch.object(control, "_scheduled_task_registered", return_value=False),
            patch.object(control, "_dispatcher_process_count", return_value=0),
        ):
            snapshot = control.component_snapshot(
                control.DEFAULT_DB,
                config,
                dashboard_running=True,
                dashboard_error=None,
            )
        self.assertEqual(snapshot["dashboard"]["status"], "HEALTHY")
        self.assertEqual(snapshot["planner"]["status"], "PENDING_WORK")
        self.assertEqual(snapshot["planner"]["pending_tasks"], 2)
        self.assertEqual(snapshot["dispatcher"]["status"], "UNDEPLOYED")
        self.assertEqual(snapshot["dispatcher"]["pending_tasks"], 1)

    def test_component_snapshot_keeps_dashboard_alive_when_task_database_is_unavailable(self) -> None:
        """任务库读取失败只降级任务监控，不能中断 Dashboard 组件。"""
        config = {
            "automations": {"planner": {"scheduled": True}},
            "codex_cli": {"dispatcher": {"task_name": "Loop Dispatcher"}},
        }
        with (
            patch.object(control, "_monitored_count", return_value=None),
            patch.object(control, "_scheduled_task_registered", return_value=True),
            patch.object(control, "_dispatcher_process_count", return_value=0),
        ):
            snapshot = control.component_snapshot(
                control.DEFAULT_DB,
                config,
                dashboard_running=True,
                dashboard_error=None,
            )
        self.assertEqual(snapshot["dashboard"]["status"], "HEALTHY")
        self.assertEqual(snapshot["planner"]["status"], "UNAVAILABLE")
        self.assertEqual(snapshot["dispatcher"]["status"], "UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
