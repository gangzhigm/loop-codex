from __future__ import annotations

import argparse
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _bootstrap import REPOSITORY_ROOT

from loopdb import load_initialization_config
from runner.agent_runtime import _launch_validation_workers
from runner.validation import ValidationWorkerSettings, is_validation_task
from runner.verification_worker import run_validation_worker


class _Process:
    pid = 31415

    def poll(self) -> None:
        return None

    def communicate(self) -> tuple[str, None]:
        return ("", None)


class RunnerValidationTests(unittest.TestCase):
    def test_validation_task_requires_prefix_and_exact_marker_line(self) -> None:
        settings = ValidationWorkerSettings.from_config(load_initialization_config())
        self.assertTrue(is_validation_task("VALIDATION-QUEUE", "说明\n[runner-validation]\n", settings))
        self.assertFalse(is_validation_task("QUEUE", "[runner-validation]", settings))
        self.assertFalse(is_validation_task("VALIDATION-QUEUE", "prefix [runner-validation]", settings))

    def test_runner_launches_only_marked_planner_candidates(self) -> None:
        snapshot = {
            "launch_enabled": True,
            "planner": {
                "candidates": [
                    {"execution_id": "planner-allowed", "task_id": "VALIDATION-ONE", "validation_eligible": True},
                    {"execution_id": "planner-blocked", "task_id": "NORMAL-ONE", "validation_eligible": False},
                ]
            },
        }
        active: dict[str, _Process] = {}
        with patch("runner.agent_runtime.subprocess.Popen", return_value=_Process()) as start:
            _launch_validation_workers(
                snapshot,
                REPOSITORY_ROOT / "data" / "loop-agent.sqlite3",
                REPOSITORY_ROOT / "config" / "initialization.json",
                active,
            )
        self.assertEqual(set(active), {"planner-allowed"})
        command = start.call_args.args[0]
        self.assertTrue(any("verification_worker.py" in value for value in command))
        self.assertIn("utf8", command)
        self.assertIn("VALIDATION-ONE", command)

    def test_validation_worker_writes_artifact_then_publishes_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifact_directory = Path(temporary)
            settings = ValidationWorkerSettings(
                enabled=True,
                task_id_prefix="VALIDATION-",
                task_marker="[runner-validation]",
                planner_ready_delay_seconds=1,
                artifact_directory=artifact_directory,
            )
            claim = {
                "outcome": "CLAIMED",
                "task": {
                    "id": "VALIDATION-FLOW",
                    "title": "验证链路",
                    "row_version": 7,
                    "operator_definition": {
                        "description": "[runner-validation]",
                        "scope_hint": ["local-agent-loop/data/runtime/validation"],
                        "estimated_capability_level": "L3",
                    },
                },
            }
            ready = {"outcome": "READY", "status": "PENDING", "preflight_status": "READY"}
            with (
                patch("runner.verification_worker.load_initialization_config", return_value=copy.deepcopy(load_initialization_config())),
                patch("runner.verification_worker.ValidationWorkerSettings.from_config", return_value=settings),
                patch("runner.verification_worker._run_loopctl", side_effect=[claim, ready]) as loopctl,
            ):
                result = run_validation_worker(
                    argparse.Namespace(
                        db=str(REPOSITORY_ROOT / "data" / "loop-agent.sqlite3"),
                        config=str(REPOSITORY_ROOT / "config" / "initialization.json"),
                        execution_id="planner-validation-flow",
                        task_id="VALIDATION-FLOW",
                    )
                )
            self.assertEqual(result["event"], "runner.validation_worker.ready")
            self.assertEqual(loopctl.call_count, 2)
            report = loopctl.call_args_list[1].kwargs["payload"]
            self.assertEqual(report["capability_level"], "L3")
            artifact = next(artifact_directory.glob("*.json"))
            self.assertIn('"stage": "READY"', artifact.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
