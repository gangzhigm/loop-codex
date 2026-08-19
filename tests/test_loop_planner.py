from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import patch

from _loop_support import *  # noqa: F403
from loopdb import CONFIG_PATH, list_tasks
from planner.main import (
    REPOSITORY_ROOT,
    handoff_selected_drafts,
    run_planner_schedule,
    start_planner_runner,
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
            self.selection_task("PROCESSING", priority="blocker", preflight_status="INSPECTING"),
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

    def test_selected_tasks_are_handed_to_distinct_runner_processes_without_ai(self) -> None:
        self.enqueue_draft("HANDOFF-A")
        self.enqueue_draft("HANDOFF-B")
        received: list[tuple[str, str, Path, Path, Path]] = []

        def start(
            task_id: str,
            execution_id: str,
            database_path: Path,
            config_path: Path,
            log_path: Path,
        ) -> int:
            received.append(
                (task_id, execution_id, database_path, config_path, log_path)
            )
            return 2000 + len(received)

        config = load_initialization_config()
        results = handoff_selected_drafts(
            self.db_path,
            CONFIG_PATH,
            config,
            start_action=start,
        )

        self.assertEqual([item[0] for item in received], ["HANDOFF-A", "HANDOFF-B"])
        self.assertEqual(len({item[1] for item in received}), 2)
        self.assertTrue(all(item[1].startswith("planner-") for item in received))
        self.assertTrue(all(result["ai_preflight_enabled"] is False for result in results))
        database = connect(self.db_path)
        states = database.execute(
            "SELECT preflight_status FROM tasks ORDER BY id"
        ).fetchall()
        preflight_count = database.execute(
            "SELECT count(*) FROM preflight_executions"
        ).fetchone()[0]
        database.close()
        self.assertEqual([row[0] for row in states], ["UNINSPECTED", "UNINSPECTED"])
        self.assertEqual(preflight_count, 0)

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

    def test_runner_launcher_passes_exact_task_and_execution_ids(self) -> None:
        captured: dict[str, object] = {}

        def launch(command, working_directory, **streams):
            captured.update(
                command=command,
                working_directory=working_directory,
                streams=streams,
            )
            return 4321

        log_path = Path(self.temporary.name) / "planner-runner.log"
        pid = start_planner_runner(
            "HANDOFF-TASK",
            "planner-handoff",
            self.db_path,
            CONFIG_PATH,
            log_path,
            launcher=launch,
        )

        self.assertEqual(pid, 4321)
        command = captured["command"]
        self.assertEqual(command[command.index("--task-id") + 1], "HANDOFF-TASK")
        self.assertEqual(
            command[command.index("--execution-id") + 1], "planner-handoff"
        )
        self.assertEqual(command[command.index("--log") + 1], str(log_path))
        self.assertEqual(captured["working_directory"], REPOSITORY_ROOT)
        self.assertEqual(captured["streams"], {})

    def test_targeted_preflight_claim_and_ready_publish_worker_contract(self) -> None:
        self.enqueue_draft("PLANNER-FIRST")
        self.enqueue_draft("PLANNER-TARGET")

        claimed = self.run_ctl(
            "preflight-claim",
            "planner-targeted",
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
            "planner-targeted",
            "PLANNER-TARGET",
            "--expected-row-version",
            str(claimed["task"]["row_version"]),
        )
        ready = self.run_ctl(
            "preflight-ready",
            "planner-targeted",
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

        result = self.run_ctl(
            "preflight-claim",
            "planner-missing",
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
        self.assertEqual(task["preflight_status"], "UNINSPECTED")


if __name__ == "__main__":
    unittest.main()
