"""Supervisor 常驻入口和组件配置契约测试。"""

from __future__ import annotations

import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from _bootstrap import REPOSITORY_ROOT

from common.components import component_specs
from common.paths import HEARTBEAT_PATH, PID_PATH, SERVER_LOG, SUPERVISOR_STOP_REQUEST
from common.service_runtime import ServiceRuntimeFiles
from loopdb import load_initialization_config
from supervisor import main as control


class SupervisorControlTests(unittest.TestCase):
    """验证 Supervisor 只解析 serve 并管理两个独立服务进程。"""

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
        dashboard, planner = component_specs(load_initialization_config())
        self.assertTrue(dashboard.enabled)
        self.assertTrue(planner.enabled)
        self.assertEqual(dashboard.entry, REPOSITORY_ROOT / "client" / "dashboard_server.py")
        self.assertEqual(dashboard.arguments, ())
        self.assertEqual(planner.entry, REPOSITORY_ROOT / "planner" / "main.py")
        self.assertEqual(planner.arguments, ("serve",))

    def test_planner_runs_when_either_scheduler_is_enabled(self) -> None:
        config = copy.deepcopy(load_initialization_config())
        config["planner"]["scheduler"]["scheduled"] = False
        dashboard, planner = component_specs(config)
        self.assertTrue(dashboard.enabled)
        self.assertTrue(planner.enabled)

        config["planner"]["execution_scheduler"]["scheduled"] = False
        _dashboard, planner = component_specs(config)
        self.assertFalse(planner.enabled)

    def test_service_runtime_owns_pid_heartbeat_stop_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = ServiceRuntimeFiles(
                component="test-service",
                pid_path=root / "service.pid",
                heartbeat_path=root / "service-heartbeat.json",
                stop_path=root / "service-stop.json",
                log_path=root / "service.log",
            )
            runtime.prepare()
            runtime.claim(12345, "duplicate")
            runtime.write_heartbeat(12345)
            runtime.request_stop(12345)

            self.assertEqual(runtime.recorded_pid(), 12345)
            self.assertIsNone(runtime.heartbeat_problem(12345, 60))
            self.assertTrue(runtime.stop_requested(12345))
            self.assertFalse(runtime.stop_requested(54321))
            shutdown_event = threading.Event()
            self.assertTrue(runtime.wait(shutdown_event, 12345, 1))
            self.assertTrue(shutdown_event.is_set())

            runtime.cleanup(12345)
            self.assertFalse(runtime.pid_path.exists())
            self.assertFalse(runtime.heartbeat_path.exists())
            self.assertFalse(runtime.stop_path.exists())

    def test_supervisor_uses_the_common_runtime_contract(self) -> None:
        runtime = ServiceRuntimeFiles.supervisor()
        self.assertEqual(runtime.component, "supervisor")
        self.assertEqual(runtime.pid_path, PID_PATH)
        self.assertEqual(runtime.heartbeat_path, HEARTBEAT_PATH)
        self.assertEqual(runtime.stop_path, SUPERVISOR_STOP_REQUEST)
        self.assertEqual(runtime.log_path, SERVER_LOG)

    def test_duplicate_claim_keeps_existing_instance_stop_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = ServiceRuntimeFiles(
                component="test-service",
                pid_path=root / "service.pid",
                heartbeat_path=root / "service-heartbeat.json",
                stop_path=root / "service-stop.json",
                log_path=root / "service.log",
            )
            runtime.prepare()
            runtime.claim(12345, "duplicate")
            runtime.request_stop(12345)

            duplicate = ServiceRuntimeFiles(
                component="test-service",
                pid_path=runtime.pid_path,
                heartbeat_path=runtime.heartbeat_path,
                stop_path=runtime.stop_path,
                log_path=runtime.log_path,
            )
            duplicate.prepare()
            with self.assertRaisesRegex(SystemExit, "duplicate"):
                duplicate.claim(54321, "duplicate")

            self.assertTrue(runtime.stop_requested(12345))


if __name__ == "__main__":
    unittest.main()
