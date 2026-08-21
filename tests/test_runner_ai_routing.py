from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from _loop_support import *  # noqa: F403
from loop_agent.runtime.core import ExecutionProfile
from loopdb import CONFIG_PATH
from runner.agent_runtime import _launch_ai_workers, _worker_command, queue_snapshot
from scheduler.execution_dispatch import (
    EventLogger,
    ExecutionDispatcher,
    ExecutionDispatchSettings,
)
from worker.codex_cli_runtime import CodexCliSettings, _codex_command
from worker.planner_codex_runtime import (
    _parse_result,
    _planner_codex_command,
)


class RunnerAiRoutingTests(LoopTestCase):
    def test_planner_candidate_uses_worker_profile_not_task_estimate(self) -> None:
        config = load_initialization_config()
        self.enqueue_draft("PLANNER-LOW-ESTIMATE", capability="L1")
        self.run_ctl("schedule-preflight", "--config", str(CONFIG_PATH))

        candidate = queue_snapshot(self.db_path, config)["planner"]["candidates"][0]

        self.assertEqual(candidate["execution_kind"], "PLANNER")
        self.assertEqual(candidate["runtime_environment"], "codex_cli")
        self.assertIsNone(candidate["provider_id"])
        self.assertEqual(candidate["capability_level"], "L3")

    def test_dispatcher_preserves_codex_task_route_in_execution_snapshot(self) -> None:
        config = load_initialization_config()
        self.add_task(
            "CODEX-WORKER",
            "project-1",
            capability_level="L4",
            runtime_environment="codex_cli",
            provider_id=None,
        )
        settings = replace(
            ExecutionDispatchSettings.from_config(config), database_path=self.db_path
        )

        result = ExecutionDispatcher(
            settings,
            config,
            execution_id_factory=lambda _level: "worker-codex-l4",
            logger=EventLogger(None),
        ).run()

        database = connect(self.db_path)
        execution = database.execute(
            "SELECT runtime_environment, provider_id, capability_level, model, reasoning "
            "FROM executions WHERE execution_id='worker-codex-l4'"
        ).fetchone()
        database.close()
        self.assertEqual(result["outcome"], "QUEUED")
        self.assertEqual(execution["runtime_environment"], "codex_cli")
        self.assertIsNone(execution["provider_id"])
        self.assertEqual(execution["capability_level"], "L4")
        self.assertEqual(execution["model"], "gpt-5.6-terra")
        self.assertEqual(execution["reasoning"], "xhigh")

    def test_runner_builds_distinct_planner_and_worker_commands(self) -> None:
        config = load_initialization_config()
        database = BASE_DIR / "data" / "loop-agent.sqlite3"
        config_path = BASE_DIR / "config" / "initialization.json"
        planner = _worker_command(
            {
                "execution_id": "planner-one",
                "execution_kind": "PLANNER",
                "capability_level": "L3",
                "runtime_environment": "codex_cli",
                "provider_id": None,
            },
            database,
            config_path,
            config,
        )
        codex = _worker_command(
            {
                "execution_id": "worker-one",
                "execution_kind": "WORKER",
                "capability_level": "L4",
                "runtime_environment": "codex_cli",
                "provider_id": None,
            },
            database,
            config_path,
            config,
        )
        self_hosted = _worker_command(
            {
                "execution_id": "worker-two",
                "execution_kind": "WORKER",
                "capability_level": "L2",
                "runtime_environment": "self_hosted_agent",
                "provider_id": "deepseek",
            },
            database,
            config_path,
            config,
        )

        self.assertTrue(any("planner_codex_runtime.py" in item for item in planner))
        self.assertTrue(any("codex_cli_runtime.py" in item for item in codex))
        self.assertTrue(any("agent_runtime.py" in item for item in self_hosted))
        self.assertIn("planner-one", planner)
        self.assertIn("worker-one", codex)
        self.assertIn("worker-two", self_hosted)

    def test_runner_launcher_starts_distinct_planner_and_formal_workers(self) -> None:
        config = load_initialization_config()
        snapshot = {
            "launch_enabled": True,
            "validation_worker": {"enabled": False},
            "planner": {
                "candidates": [{
                    "execution_id": "planner-launch",
                    "task_id": "PLAN-1",
                    "execution_kind": "PLANNER",
                    "runtime_environment": "codex_cli",
                    "provider_id": None,
                    "capability_level": "L3",
                }]
            },
            "worker": {
                "candidates": [{
                    "execution_id": "worker-launch",
                    "task_id": "WORK-1",
                    "execution_kind": "WORKER",
                    "runtime_environment": "codex_cli",
                    "provider_id": None,
                    "capability_level": "L4",
                }]
            },
        }
        children: dict[str, object] = {}
        processes = [Mock(pid=101), Mock(pid=102)]
        for process in processes:
            process.poll.return_value = None

        with patch("runner.agent_runtime.subprocess.Popen", side_effect=processes) as popen:
            _launch_ai_workers(
                snapshot,
                self.db_path,
                CONFIG_PATH,
                config,
                children,  # type: ignore[arg-type]
            )

        self.assertEqual(popen.call_count, 2)
        commands = [call.args[0] for call in popen.call_args_list]
        self.assertTrue(any("planner_codex_runtime.py" in " ".join(command) for command in commands))
        self.assertTrue(any("codex_cli_runtime.py" in " ".join(command) for command in commands))
        self.assertEqual(set(children), {"planner-launch", "worker-launch"})

    def test_codex_worker_commands_enforce_distinct_sandboxes(self) -> None:
        config = load_initialization_config()
        settings = CodexCliSettings.from_config(
            config, command_prefix=("codex",)
        )
        formal_profile = ExecutionProfile.resolve(config, "codex_cli", None, "L4")
        planner_profile = ExecutionProfile.resolve(config, "codex_cli", None, "L3")
        workspace = Path(config["workspace"]["task_root"])
        schema = Path("result.schema.json")

        formal = _codex_command(settings, formal_profile, workspace, schema)
        planner = _planner_codex_command(settings, planner_profile, workspace, schema)

        self.assertEqual(formal[formal.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(planner[planner.index("--sandbox") + 1], "read-only")
        self.assertIn("--ignore-rules", planner)
        self.assertIn("--ignore-rules", formal)
        self.assertNotIn("--ignore-user-config", planner)
        self.assertNotIn("--ignore-user-config", formal)
        self.assertIn("mcp_servers={}", planner)
        self.assertIn("mcp_servers={}", formal)
        self.assertIn("plugins", planner)
        self.assertIn("plugins", formal)

    def test_dispatcher_stops_at_public_queue_maximum(self) -> None:
        config = json.loads(json.dumps(load_initialization_config()))
        config["task_execution"]["global_max_active_executions"] = 1
        config["task_execution"]["max_queued_executions"] = 1
        self.add_task("QUEUE-FIRST", "project-1", priority="high")
        self.add_task("QUEUE-SECOND", "project-2", priority="medium")
        settings = replace(
            ExecutionDispatchSettings.from_config(config),
            database_path=self.db_path,
            max_tasks_per_cycle=3,
        )
        dispatcher = ExecutionDispatcher(
            settings,
            config,
            execution_id_factory=lambda _level: "queue-only-execution",
            logger=EventLogger(None),
        )

        first = dispatcher.run()
        second = dispatcher.run()

        self.assertEqual(first["outcome"], "QUEUED")
        self.assertEqual(first["queued_count"], 1)
        self.assertEqual(second["outcome"], "QUEUE_FULL")
        database = connect(self.db_path)
        statuses = dict(
            database.execute(
                "SELECT id, status FROM tasks WHERE id IN ('QUEUE-FIRST', 'QUEUE-SECOND')"
            ).fetchall()
        )
        database.close()
        self.assertEqual(statuses, {"QUEUE-FIRST": "QUEUED", "QUEUE-SECOND": "PENDING"})

    def test_planner_codex_jsonl_result_is_host_validated(self) -> None:
        result = {
            "outcome": "READY",
            "summary": "ready",
            "capability_level": "L2",
            "scope": ["project-1/module"],
            "lock_mode": "module",
            "technical_acceptance": ["tests pass"],
            "evidence": ["read-only inspection"],
            "question": None,
            "options": [],
            "split_suggestions": [],
            "error": None,
        }
        output = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": json.dumps(result)},
            }
        )

        outcome, report = _parse_result(output)

        self.assertEqual(outcome, "READY")
        self.assertEqual(report["capability_level"], "L2")
        self.assertNotIn("question", report)
