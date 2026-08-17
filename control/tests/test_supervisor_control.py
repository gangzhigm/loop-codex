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
    """验证 Supervisor 只解析 serve 并按配置管理两个 Scheduler。"""

    def test_serve_arguments_forward_explicit_host_and_port(self) -> None:
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

    def test_main_dispatches_serve(self) -> None:
        with patch.object(control, "serve_supervisor") as serve:
            control.main(["serve", "--port", "4999"])
        serve.assert_called_once()
        self.assertEqual(serve.call_args.args[0].port, 4999)

    def test_component_specs_read_scheduler_switches(self) -> None:
        planner, dispatcher = component_specs(load_initialization_config())
        self.assertTrue(planner.enabled)
        self.assertTrue(dispatcher.enabled)
        self.assertEqual(planner.entry, REPOSITORY_ROOT / "planner" / "main.py")
        self.assertEqual(dispatcher.entry, REPOSITORY_ROOT / "dispatcher" / "main.py")

    def test_planner_switch_does_not_change_dispatcher(self) -> None:
        config = copy.deepcopy(load_initialization_config())
        config["planner"]["scheduler"]["scheduled"] = False
        planner, dispatcher = component_specs(config)
        self.assertFalse(planner.enabled)
        self.assertTrue(dispatcher.enabled)


if __name__ == "__main__":
    unittest.main()
