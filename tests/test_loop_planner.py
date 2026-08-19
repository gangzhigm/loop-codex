from __future__ import annotations

import threading
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from _loop_support import *  # noqa: F403
from loopdb import CONFIG_PATH, list_tasks
from planner.main import (
    REPOSITORY_ROOT,
    run_planner_schedule,
    schedule_preflights,
)
from planner.execution_dispatch import (
    EventLogger,
    ExecutionDispatcher,
    ExecutionDispatchSettings,
    select_candidate,
)
from planner.task_query import load_draft_tasks, select_draft_tasks
from runner.planner_runner import receive_planner_task


class _Clock:
    def __init__(self) -> None:
        self.current = 0.0

    def monotonic(self) -> float:
        return self.current


class _Runtime:
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.heartbeats: list[float] = []

    def stop_requested(self, pid: int) -> bool:
        del pid
        return False

    def write_heartbeat(self, pid: int) -> None:
        del pid
        self.heartbeats.append(self.clock.current)

    def wait(
        self,
        shutdown_event: threading.Event,
        pid: int,
        timeout_seconds: float,
    ) -> bool:
        del shutdown_event, pid
        self.clock.current += timeout_seconds
        return False


class PlannerReadOnlyDiscoveryTests(LoopTestCase):
    @staticmethod
    def selection_task(
        task_id: str,
        *,
        priority: str,
        preflight_status: str = "UNINSPECTED",
        created_at: str = "2026-08-19T10:00:00+08:00",
    ) -> dict[str, object]:
        return {
            "id": task_id,
            "status": "DRAFT",
            "preflight_status": preflight_status,
            "priority": priority,
            "created_at": created_at,
        }

    def test_draft_query_returns_complete_read_only_task_projections(self) -> None:
        self.enqueue_draft("DRAFT-A")
        self.enqueue_draft("DRAFT-B")
        self.add_task("READY-C", "project-1")
        database = connect(self.db_path)
        before = database.execute(
            "SELECT id, status, preflight_status, row_version FROM tasks ORDER BY id"
        ).fetchall()
        history_count = database.execute("SELECT count(*) FROM task_history").fetchone()[0]
        database.close()

        tasks = load_draft_tasks(self.db_path)

        self.assertEqual([task["id"] for task in tasks], ["DRAFT-A", "DRAFT-B"])
        self.assertTrue(all(task["status"] == "DRAFT" for task in tasks))
        self.assertTrue(all("operator_definition" in task for task in tasks))
        database = connect(self.db_path)
        after = database.execute(
            "SELECT id, status, preflight_status, row_version FROM tasks ORDER BY id"
        ).fetchall()
        after_history_count = database.execute("SELECT count(*) FROM task_history").fetchone()[0]
        database.close()
        self.assertEqual([tuple(row) for row in after], [tuple(row) for row in before])
        self.assertEqual(after_history_count, history_count)

    def test_shared_task_query_filters_in_database_and_preserves_all_tasks(self) -> None:
        self.enqueue_draft("FILTER-DRAFT")
        self.add_task("FILTER-READY", "project-1")
        database = connect(self.db_path)
        try:
            self.assertEqual(
                [task["id"] for task in list_tasks(database, statuses={"DRAFT"})],
                ["FILTER-DRAFT"],
            )
            self.assertEqual(
                {task["id"] for task in all_tasks(database)},
                {"FILTER-DRAFT", "FILTER-READY"},
            )
            self.assertEqual(list_tasks(database, statuses=set()), [])
        finally:
            database.close()

    def test_selection_fills_only_available_slots_using_configured_priority(self) -> None:
        drafts = [
            self.selection_task("LOW-OLD", priority="low"),
            self.selection_task("CRITICAL-NEW", priority="critical", created_at="2026-08-19T11:00:00+08:00"),
            self.selection_task("CRITICAL-OLD", priority="critical"),
            self.selection_task("QUEUED", priority="blocker", preflight_status="QUEUED"),
        ]
        with patch("planner.task_query.load_draft_tasks", return_value=drafts):
            selection = select_draft_tasks(
                self.db_path,
                max_active_executions=3,
                priority_levels=["blocker", "critical", "high", "medium", "low"],
            )

        self.assertEqual(selection["draft_total"], 4)
        self.assertEqual(selection["processing_count"], 1)
        self.assertEqual(selection["available_slots"], 2)
        self.assertEqual(
            [task["id"] for task in selection["selected_tasks"]],
            ["CRITICAL-OLD", "CRITICAL-NEW"],
        )

    def test_selection_returns_no_tasks_when_configured_capacity_is_full(self) -> None:
        drafts = [
            self.selection_task("PROCESSING-A", priority="critical", preflight_status="INSPECTING"),
            self.selection_task("PROCESSING-B", priority="medium", preflight_status="INSPECTING"),
            self.selection_task("WAITING", priority="blocker"),
        ]
        with patch("planner.task_query.load_draft_tasks", return_value=drafts):
            selection = select_draft_tasks(
                self.db_path,
                max_active_executions=2,
                priority_levels=["blocker", "critical", "high", "medium", "low"],
            )

        self.assertEqual(selection["processing_count"], 2)
        self.assertEqual(selection["available_slots"], 0)
        self.assertEqual(selection["selected_count"], 0)
        self.assertEqual(selection["selected_tasks"], [])

    def test_schedule_queries_immediately_then_at_configured_interval(self) -> None:
        clock = _Clock()
        runtime = _Runtime(clock)
        shutdown_event = threading.Event()
        query_times: list[float] = []

        def query() -> None:
            query_times.append(clock.current)
            if len(query_times) == 2:
                shutdown_event.set()

        run_planner_schedule(
            runtime, 12345, shutdown_event,
            heartbeat_interval_seconds=15,
            query_interval_seconds=300,
            query_action=query,
            monotonic=clock.monotonic,
        )

        self.assertEqual(query_times, [0.0, 300.0])
        self.assertEqual(runtime.heartbeats[0], 0.0)
        self.assertEqual(runtime.heartbeats[-1], 300.0)

    def test_preflight_and_execution_schedules_use_independent_clocks(self) -> None:
        clock = _Clock()
        runtime = _Runtime(clock)
        shutdown_event = threading.Event()
        preflight_times: list[float] = []
        execution_times: list[float] = []

        def preflight() -> None:
            preflight_times.append(clock.current)

        def execute() -> None:
            execution_times.append(clock.current)
            if len(execution_times) == 2:
                shutdown_event.set()

        run_planner_schedule(
            runtime,
            12345,
            shutdown_event,
            heartbeat_interval_seconds=15,
            query_interval_seconds=300,
            query_action=preflight,
            execution_interval_seconds=900,
            execution_action=execute,
            monotonic=clock.monotonic,
        )

        self.assertEqual(preflight_times, [0.0, 300.0, 600.0, 900.0])
        self.assertEqual(execution_times, [0.0, 900.0])

    def test_execution_candidate_requires_pending_ready(self) -> None:
        base = {
            "runtime_environment": "self_hosted_agent",
            "provider_id": "deepseek",
            "capability_level": "L3",
            "execution_policy": "automatic",
            "depends_on": [],
        }
        tasks = [
            {**base, "id": "NOT-READY", "status": "PENDING", "preflight_status": "FAILED"},
            {**base, "id": "READY", "status": "PENDING", "preflight_status": "READY"},
        ]

        selected = select_candidate(tasks, "deepseek", ("L3",))

        self.assertEqual(selected["id"], "READY")

    def test_execution_dispatch_starts_agent_runner_without_claiming(self) -> None:
        config = load_initialization_config()
        settings = replace(
            ExecutionDispatchSettings.from_config(config),
            database_path=self.db_path,
        )
        task = {
            "id": "EXECUTION-READY",
            "status": "PENDING",
            "preflight_status": "READY",
            "runtime_environment": "self_hosted_agent",
            "provider_id": "deepseek",
            "capability_level": "L3",
            "execution_policy": "automatic",
            "depends_on": [],
        }
        launched: dict[str, object] = {}

        def launch(command: list[str], cwd: Path) -> int:
            launched.update(command=command, cwd=cwd)
            return 24680

        result = ExecutionDispatcher(
            settings,
            config,
            snapshot_reader=lambda _settings, _config: ([task], []),
            launcher=launch,
            logger=EventLogger(None),
        ).run()

        command = launched["command"]
        self.assertEqual(result["outcome"], "RUNNER_STARTED")
        self.assertEqual(result["candidate_task_id"], "EXECUTION-READY")
        self.assertEqual(result["runner_pid"], 24680)
        self.assertIn(str(REPOSITORY_ROOT / "runner" / "agent_runtime.py"), command)
        self.assertNotIn("claim", command)

    def test_execution_dispatch_respects_global_and_platform_capacity(self) -> None:
        config = load_initialization_config()
        settings = replace(
            ExecutionDispatchSettings.from_config(config),
            database_path=self.db_path,
            global_max_active_executions=1,
            platform_max_active_executions=1,
        )
        task = {
            "id": "CAPACITY-READY",
            "status": "PENDING",
            "preflight_status": "READY",
            "runtime_environment": "self_hosted_agent",
            "provider_id": "deepseek",
            "capability_level": "L3",
            "execution_policy": "automatic",
            "depends_on": [],
        }

        global_result = ExecutionDispatcher(
            settings,
            config,
            snapshot_reader=lambda _settings, _config: (
                [task],
                [{"runtime_environment": "self_hosted_agent"}],
            ),
            launcher=lambda _command, _cwd: self.fail("容量已满时不得启动 Runner"),
            logger=EventLogger(None),
        ).run()
        platform_result = ExecutionDispatcher(
            replace(settings, global_max_active_executions=2),
            config,
            snapshot_reader=lambda _settings, _config: (
                [task],
                [{"runtime_environment": "self_hosted_agent"}],
            ),
            launcher=lambda _command, _cwd: self.fail("容量已满时不得启动 Runner"),
            logger=EventLogger(None),
        ).run()

        self.assertEqual((global_result["outcome"], global_result["limit_scope"]), ("SLOT_FULL", "global"))
        self.assertEqual((platform_result["outcome"], platform_result["limit_scope"]), ("SLOT_FULL", "platform"))

    def test_schedule_atomically_queues_only_configured_capacity(self) -> None:
        for task_id in ("QUEUE-A", "QUEUE-B", "QUEUE-C"):
            self.enqueue_draft(task_id)

        result = self.run_ctl("schedule-preflight", "--config", str(CONFIG_PATH))

        self.assertEqual(result["outcome"], "QUEUED")
        self.assertEqual(result["queued_count"], 2)
        database = connect(self.db_path)
        states = database.execute(
            "SELECT id, preflight_status, preflight_execution_id FROM tasks ORDER BY id"
        ).fetchall()
        executions = database.execute(
            "SELECT execution_id, task_id, status FROM preflight_executions ORDER BY task_id"
        ).fetchall()
        validation = validate_database(database)
        database.close()
        self.assertEqual(
            [row["preflight_status"] for row in states],
            ["QUEUED", "QUEUED", "UNINSPECTED"],
        )
        self.assertEqual([row["status"] for row in executions], ["QUEUED", "QUEUED"])
        self.assertEqual(
            {row["preflight_execution_id"] for row in states[:2]},
            {row["execution_id"] for row in executions},
        )
        self.assertTrue(validation["ok"], validation["errors"])

    def test_repeated_schedule_does_not_duplicate_queued_tasks(self) -> None:
        self.enqueue_draft("QUEUE-ONCE")
        first = self.run_ctl("schedule-preflight", "--config", str(CONFIG_PATH))
        second = self.run_ctl("schedule-preflight", "--config", str(CONFIG_PATH))

        self.assertEqual(first["queued_count"], 1)
        self.assertEqual(second["queued_count"], 0)
        database = connect(self.db_path)
        count = database.execute(
            "SELECT count(*) FROM preflight_executions WHERE task_id='QUEUE-ONCE'"
        ).fetchone()[0]
        database.close()
        self.assertEqual(count, 1)

    def test_scheduler_calls_loopctl_without_starting_runner(self) -> None:
        captured: dict[str, object] = {}

        def run(command, **options):
            captured.update(command=command, options=options)
            return subprocess.CompletedProcess(
                command,
                0,
                stdout='{"outcome":"NO_TASK","queued_count":0}',
                stderr="",
            )

        result = schedule_preflights(
            self.db_path,
            CONFIG_PATH,
            command_runner=run,
        )

        command = captured["command"]
        self.assertEqual(result["event"], "planner.preflight_schedule.completed")
        self.assertIn(str(REPOSITORY_ROOT / "control" / "loopctl.py"), command)
        self.assertIn("schedule-preflight", command)
        self.assertNotIn("planner_runner.py", " ".join(command))

    def test_planner_runner_receipt_is_read_only_and_stops_before_ai(self) -> None:
        self.enqueue_draft("HANDOFF-RECEIVER")
        database = connect(self.db_path)
        before = database.execute(
            "SELECT status, preflight_status, row_version FROM tasks "
            "WHERE id='HANDOFF-RECEIVER'"
        ).fetchone()
        database.close()

        result = receive_planner_task(
            self.db_path,
            load_initialization_config(),
            execution_id="planner-receiver",
            task_id="HANDOFF-RECEIVER",
        )

        self.assertEqual(result["outcome"], "PLANNER_TASK_RECEIVED")
        self.assertEqual(result["next_action"], "AI_PREFLIGHT_NOT_ENABLED")
        database = connect(self.db_path)
        after = database.execute(
            "SELECT status, preflight_status, row_version FROM tasks "
            "WHERE id='HANDOFF-RECEIVER'"
        ).fetchone()
        preflight_count = database.execute(
            "SELECT count(*) FROM preflight_executions"
        ).fetchone()[0]
        database.close()
        self.assertEqual(tuple(after), tuple(before))
        self.assertEqual(preflight_count, 0)

    def test_targeted_preflight_claim_and_ready_publish_worker_contract(self) -> None:
        self.enqueue_draft("PLANNER-FIRST")
        self.enqueue_draft("PLANNER-TARGET")
        scheduled = self.run_ctl(
            "schedule-preflight", "--config", str(CONFIG_PATH)
        )
        execution_id = next(
            item["execution_id"]
            for item in scheduled["queued"]
            if item["task_id"] == "PLANNER-TARGET"
        )

        claimed = self.run_ctl(
            "preflight-claim",
            execution_id,
            "--task-id",
            "PLANNER-TARGET",
            "--runtime-environment",
            "self_hosted_agent",
            "--sandbox",
            "read-only",
        )

        self.assertEqual(claimed["outcome"], "CLAIMED")
        self.assertEqual(claimed["task_id"], "PLANNER-TARGET")
        self.assertEqual(claimed["task"]["preflight_status"], "INSPECTING")
        heartbeat = self.run_ctl(
            "preflight-heartbeat",
            execution_id,
            "PLANNER-TARGET",
            "--expected-row-version",
            str(claimed["task"]["row_version"]),
        )
        ready = self.run_ctl(
            "preflight-ready",
            execution_id,
            "PLANNER-TARGET",
            "--expected-row-version",
            str(heartbeat["row_version"]),
            input_text=self.ready_report(),
        )
        self.assertEqual((ready["status"], ready["preflight_status"]), ("PENDING", "READY"))
        worker = self.claim("worker-after-planner", "L3")
        self.assertEqual(worker["task"]["id"], "PLANNER-TARGET")

    def test_targeted_preflight_claim_does_not_substitute_another_task(self) -> None:
        self.enqueue_draft("PLANNER-AVAILABLE")
        scheduled = self.run_ctl(
            "schedule-preflight", "--config", str(CONFIG_PATH)
        )
        execution_id = scheduled["queued"][0]["execution_id"]

        result = self.run_ctl(
            "preflight-claim",
            execution_id,
            "--task-id",
            "DOES-NOT-EXIST",
            "--runtime-environment",
            "self_hosted_agent",
            "--sandbox",
            "read-only",
        )

        self.assertEqual(result["outcome"], "NO_TASK")
        database = connect(self.db_path)
        task = database.execute(
            "SELECT preflight_status FROM tasks WHERE id='PLANNER-AVAILABLE'"
        ).fetchone()
        database.close()
        self.assertEqual(task["preflight_status"], "QUEUED")

    def test_preflight_claim_cannot_bypass_planner_queue(self) -> None:
        self.enqueue_draft("NOT-QUEUED")

        result = self.run_ctl(
            "preflight-claim",
            "planner-not-queued",
            "--task-id",
            "NOT-QUEUED",
            "--runtime-environment",
            "self_hosted_agent",
            "--sandbox",
            "read-only",
        )

        self.assertEqual(result["outcome"], "NO_TASK")
        database = connect(self.db_path)
        task = database.execute(
            "SELECT preflight_status, preflight_execution_id FROM tasks "
            "WHERE id='NOT-QUEUED'"
        ).fetchone()
        execution_count = database.execute(
            "SELECT count(*) FROM preflight_executions"
        ).fetchone()[0]
        database.close()
        self.assertEqual(tuple(task), ("UNINSPECTED", None))
        self.assertEqual(execution_count, 0)


if __name__ == "__main__":
    unittest.main()
