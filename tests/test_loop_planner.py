from __future__ import annotations

import threading
from unittest.mock import patch

from _loop_support import *  # noqa: F403
from loopdb import list_tasks
from planner.main import run_planner_schedule
from planner.task_query import load_draft_tasks, select_draft_tasks


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

    def test_all_legacy_preflight_commands_fail_without_changing_database(self) -> None:
        self.enqueue_draft("PLANNER-DISABLED")
        invocations = (
            (
                "preflight-claim",
                "planner-disabled",
                "--runtime-environment",
                "self_hosted_agent",
                "--sandbox",
                "read-only",
            ),
            ("preflight-heartbeat", "planner-disabled", "PLANNER-DISABLED"),
            ("preflight-ready", "planner-disabled", "PLANNER-DISABLED"),
            ("preflight-needs-review", "planner-disabled", "PLANNER-DISABLED"),
            ("preflight-fail", "planner-disabled", "PLANNER-DISABLED"),
        )

        for arguments in invocations:
            with self.subTest(command=arguments[0]):
                result = self.run_ctl_error(*arguments, input_text="{}")
                self.assertIn("Planner 业务尚未实现", result["message"])

        database = connect(self.db_path)
        task = database.execute(
            "SELECT status, preflight_status, preflight_execution_id "
            "FROM tasks WHERE id='PLANNER-DISABLED'"
        ).fetchone()
        execution_count = database.execute(
            "SELECT count(*) FROM preflight_executions"
        ).fetchone()[0]
        database.close()
        self.assertEqual(tuple(task), ("DRAFT", "UNINSPECTED", None))
        self.assertEqual(execution_count, 0)


if __name__ == "__main__":
    unittest.main()
