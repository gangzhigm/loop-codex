from __future__ import annotations

import copy
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from codex_cli_dispatcher import (
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
    profile: str = "standard",
    status: str = "PENDING",
    dependencies: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": task_id,
        "status": status,
        "priority": priority,
        "runtime_environment": "codex_cli",
        "execution_profile": profile,
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

    def test_select_candidate_keeps_priority_order_and_supported_profiles(self) -> None:
        tasks = [
            task("medium", priority="medium"),
            task("blocked", priority="blocker", profile="exceptional"),
            task("critical", priority="critical", profile="advanced"),
        ]
        selected = select_candidate(tasks, self.settings.supported_profiles)
        self.assertEqual(selected["id"], "medium")
        selected = select_candidate(sorted(tasks, key=lambda value: {"blocker": 0, "critical": 1, "medium": 3}[str(value["priority"])]), self.settings.supported_profiles)
        self.assertEqual(selected["id"], "critical")

    def test_no_task_and_unsatisfied_dependencies_do_not_start_runner(self) -> None:
        for tasks in ([], [task("blocked", dependencies=["upstream"]), task("upstream", status="FAILED")]):
            with self.subTest(tasks=tasks):
                result = self.dispatcher(tasks).run()
                self.assertEqual(result["outcome"], "NO_TASK")
                self.assertFalse(self.launches)

    def test_global_and_profile_capacity_prevent_runner_start(self) -> None:
        selected = task("selected", profile="advanced")
        global_agents = [{"execution_profile": "standard"}] * self.settings.max_parallel_tasks
        global_result = self.dispatcher([selected], global_agents).run()
        self.assertEqual((global_result["outcome"], global_result["limit_scope"]), ("SLOT_FULL", "global"))
        self.assertFalse(self.launches)
        profile_agents = [{"execution_profile": "advanced"}] * self.settings.profile_parallel_limits["advanced"]
        profile_result = self.dispatcher([selected], profile_agents).run()
        self.assertEqual((profile_result["outcome"], profile_result["limit_scope"]), ("SLOT_FULL", "profile"))
        self.assertFalse(self.launches)

    def test_one_runner_launch_keeps_runner_claim_as_final_race_arbiter(self) -> None:
        tasks = [task("first", priority="critical", profile="advanced"), task("second", priority="high", profile="standard")]
        result = self.dispatcher(tasks).run()
        self.assertEqual(result["outcome"], "RUNNER_FINISHED")
        self.assertEqual(len(self.launches), 1)
        command, cwd, timeout = self.launches[0]
        self.assertEqual(command[command.index("--profile") + 1], "advanced")
        self.assertIn("--config", command)
        self.assertIn("--db", command)
        self.assertEqual(cwd, BASE_DIR)
        self.assertEqual(timeout, self.settings.timeout_seconds)

    def test_runner_terminal_outcomes_do_not_trigger_second_profile(self) -> None:
        runner = subprocess.CompletedProcess(["runner"], 0, stdout='{"outcome":"CONFLICT"}', stderr="")
        result = self.dispatcher([task("first", profile="advanced"), task("second", profile="standard")], result=runner).run()
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
            {"supported_execution_profiles": ["exceptional"]},
            {"working_directory": str(BASE_DIR.parent)},
        ):
            with self.subTest(patch=patch):
                config = copy.deepcopy(self.config)
                config["codex_cli"]["dispatcher"].update(patch)
                with self.assertRaises(DispatcherError):
                    DispatcherSettings.from_config(config)

    def test_install_script_dry_run_does_not_touch_task_scheduler(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is unavailable")
        completed = subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(BASE_DIR / "scripts" / "install_codex_cli_task.ps1"), "-DryRun"],
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
