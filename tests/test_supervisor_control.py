"""Supervisor 常驻入口和组件配置契约测试。"""

from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from _bootstrap import REPOSITORY_ROOT

from common.components import component_specs
from loopdb import load_initialization_config
from supervisor import main as control


class SupervisorControlTests(unittest.TestCase):
    """验证 Supervisor 只解析 serve 并管理三个独立服务进程。"""

    def test_serve_arguments_only_control_supervisor(self) -> None:
        args = control.parser().parse_args(
            [
                "serve",
                "--db",
                "test.sqlite3",
                "--config",
                "test-config.json",
                "--monitor-interval-seconds",
                "5",
            ]
        )
        self.assertEqual(args.db, "test.sqlite3")
        self.assertEqual(args.config, "test-config.json")
        self.assertEqual(args.monitor_interval_seconds, 5)

    def test_main_dispatches_serve(self) -> None:
        with patch.object(control, "serve_supervisor") as serve:
            control.main(["serve", "--monitor-interval-seconds", "5"])
        serve.assert_called_once()
        self.assertEqual(serve.call_args.args[0].monitor_interval_seconds, 5)

    def test_component_specs_read_service_switches(self) -> None:
        dashboard, planner, dispatcher = component_specs(load_initialization_config())
        self.assertTrue(dashboard.enabled)
        self.assertTrue(planner.enabled)
        self.assertTrue(dispatcher.enabled)
        self.assertEqual(dashboard.entry, REPOSITORY_ROOT / "client" / "dashboard_server.py")
        self.assertEqual(dashboard.arguments, ())
        self.assertEqual(planner.entry, REPOSITORY_ROOT / "planner" / "main.py")
        self.assertEqual(planner.arguments, ("serve",))
        self.assertEqual(dispatcher.entry, REPOSITORY_ROOT / "dispatcher" / "main.py")

    def test_planner_switch_does_not_change_dispatcher(self) -> None:
        config = copy.deepcopy(load_initialization_config())
        config["planner"]["scheduler"]["scheduled"] = False
        dashboard, planner, dispatcher = component_specs(config)
        self.assertTrue(dashboard.enabled)
        self.assertFalse(planner.enabled)
        self.assertTrue(dispatcher.enabled)


if __name__ == "__main__":
    unittest.main()
