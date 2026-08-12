from __future__ import annotations

# 中文排查：覆盖 Dispatcher 的候选选择、容量判断、配置校验和安装器 dry-run。
# 没有选中预期等级时先检查构造任务的状态、依赖和优先级，再检查排序函数。
# Dispatcher 测试不应真正启动 Codex CLI，子进程入口必须通过 mock 验证。

import copy
import json
import subprocess
import unittest
from pathlib import Path

from _bootstrap import REPOSITORY_ROOT

from dispatcher.codex_cli_dispatcher import (
    BASE_DIR,
    CodexCliDispatcher,
    DispatcherError,
    DispatcherSettings,
    EventLogger,
    select_candidate,
)
from loopdb import load_initialization_config


def task(
    task_id: str,
    *,
    priority: str = "medium",
    capability_level: str = "L2",
    status: str = "PENDING",
    dependencies: list[str] | None = None,
    execution_policy: str = "automatic",
) -> dict[str, object]:
    return {
        "id": task_id,
        "status": status,
        "priority": priority,
        "runtime_environment": "codex_cli",
        "provider_id": None,
        "capability_level": capability_level,
        "execution_policy": execution_policy,
        "depends_on": dependencies or [],
    }


class CodexCliDispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_initialization_config()
        self.settings = DispatcherSettings.from_config(self.config)
        self.launches: list[tuple[list[str], Path, float]] = []

    def dispatcher(self, tasks: list[dict[str, object]], agents: list[dict[str, object]] | None = None, *, result: object = None) -> CodexCliDispatcher:
        def snapshot(_settings: DispatcherSettings, _config: dict[str, object]):
            return tasks, agents or []

        def launcher(command: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
            self.launches.append((command, cwd, timeout))
            if isinstance(result, BaseException):
                raise result
            if isinstance(result, subprocess.CompletedProcess):
                return result
            return subprocess.CompletedProcess(command, 0, stdout='{"outcome":"NO_TASK"}', stderr="")

        return CodexCliDispatcher(self.settings, self.config, snapshot_reader=snapshot, launcher=launcher, logger=EventLogger(None))

    def test_select_candidate_keeps_priority_order_and_supported_capabilities(self) -> None:
        tasks = [
            task("medium", priority="medium"),
            task("manual", priority="blocker", capability_level="L5", execution_policy="manual"),
            task("critical", priority="critical", capability_level="L3"),
        ]
        selected = select_candidate(tasks, self.settings.supported_capability_levels)
        self.assertEqual(selected["id"], "medium")
        selected = select_candidate(
            sorted(tasks, key=lambda value: {"blocker": 0, "critical": 1, "medium": 3}[str(value["priority"])]),
            self.settings.supported_capability_levels,
        )
        self.assertEqual(selected["id"], "critical")

    def test_no_task_and_unsatisfied_dependencies_do_not_start_runner(self) -> None:
        for tasks in ([], [task("blocked", dependencies=["upstream"]), task("upstream", status="FAILED")]):
            with self.subTest(tasks=tasks):
                result = self.dispatcher(tasks).run()
                self.assertEqual(result["outcome"], "NO_TASK")
                self.assertFalse(self.launches)

    def test_global_and_platform_capacity_prevent_runner_start(self) -> None:
        selected = task("selected", capability_level="L3")
        global_agents = [
            {"runtime_environment": "codex_automation", "capability_level": "L1"}
        ] * self.settings.global_max_active_executions
        global_result = self.dispatcher([selected], global_agents).run()
        self.assertEqual((global_result["outcome"], global_result["limit_scope"]), ("SLOT_FULL", "global"))
        self.assertFalse(self.launches)
        platform_agents = [
            {"runtime_environment": "codex_cli", "capability_level": level}
            for level in ("L1", "L2", "L3", "L4", "L5")
        ]
        self.assertEqual(len(platform_agents), self.settings.platform_max_active_executions)
        platform_result = self.dispatcher([selected], platform_agents).run()
        self.assertEqual(
            (platform_result["outcome"], platform_result["limit_scope"]),
            ("SLOT_FULL", "platform"),
        )
        self.assertFalse(self.launches)

    def test_one_runner_launch_keeps_runner_claim_as_final_race_arbiter(self) -> None:
        tasks = [
            task("first", priority="critical", capability_level="L3"),
            task("second", priority="high", capability_level="L2"),
        ]
        result = self.dispatcher(tasks).run()
        self.assertEqual(result["outcome"], "RUNNER_FINISHED")
        self.assertEqual(len(self.launches), 1)
        command, cwd, timeout = self.launches[0]
        self.assertEqual(command[command.index("--capability-level") + 1], "L3")
        self.assertIn("--config", command)
        self.assertIn("--db", command)
        self.assertEqual(cwd, BASE_DIR)
        self.assertEqual(timeout, self.settings.timeout_seconds)

    def test_runner_terminal_outcomes_do_not_trigger_second_profile(self) -> None:
        runner = subprocess.CompletedProcess(["runner"], 0, stdout='{"outcome":"CONFLICT"}', stderr="")
        result = self.dispatcher(
            [task("first", capability_level="L3"), task("second", capability_level="L2")],
            result=runner,
        ).run()
        self.assertEqual(result["outcome"], "RUNNER_FINISHED")
        self.assertEqual(len(self.launches), 1)

    def test_runner_start_failure_and_timeout_are_sanitized(self) -> None:
        for error, outcome in ((OSError("private detail"), "RUNNER_START_FAILED"), (subprocess.TimeoutExpired(["runner"], 1), "RUNNER_TIMEOUT")):
            with self.subTest(outcome=outcome):
                self.launches.clear()
                result = self.dispatcher([task("selected")], result=error).run()
                self.assertEqual(result["outcome"], outcome)
                self.assertEqual(len(self.launches), 1)

    def test_dispatcher_config_rejects_unsafe_or_invalid_values(self) -> None:
        for patch in (
            {"interval_minutes": 0},
            {"supported_capability_levels": ["L6"]},
            {"working_directory": str(BASE_DIR.parent)},
        ):
            with self.subTest(patch=patch):
                config = copy.deepcopy(self.config)
                config["codex_cli"]["dispatcher"].update(patch)
                with self.assertRaises(DispatcherError):
                    DispatcherSettings.from_config(config)

    def test_dispatcher_timeout_covers_all_configured_attempts(self) -> None:
        config = copy.deepcopy(self.config)
        config["codex_cli"]["dispatcher"]["timeout_seconds"] = 7200
        with self.assertRaisesRegex(DispatcherError, "complete execution attempt"):
            DispatcherSettings.from_config(config)

    def test_missing_capability_profile_prevents_dispatch(self) -> None:
        config = copy.deepcopy(self.config)
        del config["execution_profiles"]["codex_cli"]["capabilities"]["L3"]
        with self.assertRaises(DispatcherError):
            DispatcherSettings.from_config(config)

    def test_install_script_dry_run_does_not_touch_task_scheduler(self) -> None:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(BASE_DIR / "control" / "installers" / "install_codex_cli_task.ps1"), "-DryRun"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["outcome"], "DRY_RUN")
        self.assertTrue(payload["run_as_current_user"])
        self.assertTrue(payload["hidden"])


if __name__ == "__main__":
    unittest.main()
