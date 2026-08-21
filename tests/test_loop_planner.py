from __future__ import annotations

import threading
import subprocess
from dataclasses import replace

from _loop_support import *  # noqa: F403
from loopdb import CONFIG_PATH, list_tasks
from scheduler.main import (
    REPOSITORY_ROOT,
    run_scheduler,
    schedule_preflights,
)
from scheduler.execution_dispatch import (
    EventLogger,
    ExecutionDispatcher,
    ExecutionDispatchSettings,
    select_candidate,
)
from runner.agent_runtime import queue_snapshot


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


class SchedulerAndPlannerTests(LoopTestCase):
    def test_shared_task_projection_filters_status_and_preserves_all_tasks(self) -> None:
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

    def test_schedule_queries_immediately_then_at_configured_interval(self) -> None:
        clock = _Clock()
        runtime = _Runtime(clock)
        shutdown_event = threading.Event()
        query_times: list[float] = []

        def query() -> None:
            query_times.append(clock.current)
            if len(query_times) == 2:
                shutdown_event.set()

        run_scheduler(
            runtime, 12345, shutdown_event,
            heartbeat_interval_seconds=15,
            preflight_interval_seconds=300,
            preflight_action=query,
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

        run_scheduler(
            runtime,
            12345,
            shutdown_event,
            heartbeat_interval_seconds=15,
            preflight_interval_seconds=300,
            preflight_action=preflight,
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
            "lock_mode": "project",
            "scope": ["project-1/file.txt"],
            "planner_supplement": {
                "technical_acceptance": ["verified"],
                "evidence": ["inspected"],
            },
        }
        tasks = [
            {**base, "id": "NOT-READY", "status": "PENDING", "preflight_status": "FAILED"},
            {**base, "id": "READY", "status": "PENDING", "preflight_status": "READY"},
        ]

        selected = select_candidate(tasks, "deepseek", ("L3",))

        self.assertEqual(selected["id"], "READY")

    def test_execution_dispatch_atomically_queues_worker_execution(self) -> None:
        config = load_initialization_config()
        settings = replace(
            ExecutionDispatchSettings.from_config(config),
            database_path=self.db_path,
        )
        self.add_task("EXECUTION-READY", "project-1", capability_level="L3")

        result = ExecutionDispatcher(
            settings,
            config,
            execution_id_factory=lambda _level: "worker-l3-queued",
            logger=EventLogger(None),
        ).run()

        database = connect(self.db_path)
        task = database.execute(
            "SELECT status, assigned_agent FROM tasks WHERE id='EXECUTION-READY'"
        ).fetchone()
        execution = database.execute(
            "SELECT task_id, status, execution_kind FROM executions "
            "WHERE execution_id='worker-l3-queued'"
        ).fetchone()
        database.close()
        self.assertEqual(result["outcome"], "QUEUED")
        self.assertEqual(tuple(task), ("QUEUED", None))
        self.assertEqual(tuple(execution), ("EXECUTION-READY", "QUEUED", "WORKER"))

    def test_execution_dispatch_does_not_queue_the_same_task_twice(self) -> None:
        config = load_initialization_config()
        settings = replace(
            ExecutionDispatchSettings.from_config(config),
            database_path=self.db_path,
        )
        self.add_task("QUEUE-ONCE-WORKER", "project-1", capability_level="L3")
        first = ExecutionDispatcher(
            settings, config,
            execution_id_factory=lambda _level: "worker-queue-once",
            logger=EventLogger(None),
        ).run()
        second = ExecutionDispatcher(settings, config, logger=EventLogger(None)).run()
        database = connect(self.db_path)
        count = database.execute(
            "SELECT count(*) FROM executions WHERE task_id='QUEUE-ONCE-WORKER'"
        ).fetchone()[0]
        database.close()
        self.assertEqual(first["outcome"], "QUEUED")
        self.assertEqual(second["outcome"], "NO_TASK")
        self.assertEqual(count, 1)

    def test_unschedule_execution_returns_unclaimed_task_to_pending(self) -> None:
        config = load_initialization_config()
        settings = replace(
            ExecutionDispatchSettings.from_config(config),
            database_path=self.db_path,
        )
        self.add_task("WORKER-WITHDRAW", "project-1", capability_level="L3")
        ExecutionDispatcher(
            settings,
            config,
            execution_id_factory=lambda _level: "worker-withdraw-queued",
            logger=EventLogger(None),
        ).run()

        result = self.run_ctl(
            "unschedule-execution",
            "WORKER-WITHDRAW",
            "--expected-row-version",
            "2",
            "--reason",
            "测试撤回正式 AI 队列。",
        )

        database = connect(self.db_path)
        task = database.execute(
            "SELECT status, preflight_status, row_version FROM tasks "
            "WHERE id='WORKER-WITHDRAW'"
        ).fetchone()
        execution = database.execute(
            "SELECT status, outcome, finished_at, termination_reason FROM executions "
            "WHERE execution_id='worker-withdraw-queued'"
        ).fetchone()
        validation = validate_database(database)
        database.close()
        self.assertEqual(result["outcome"], "EXECUTION_UNSCHEDULED")
        self.assertEqual(tuple(task), ("PENDING", "READY", 3))
        self.assertEqual(execution["status"], "FINISHED")
        self.assertIsNone(execution["outcome"])
        self.assertIsNotNone(execution["finished_at"])
        self.assertEqual(execution["termination_reason"], "测试撤回正式 AI 队列。")
        self.assertTrue(validation["ok"], validation["errors"])

    def test_runner_selects_both_queue_kinds_without_launching(self) -> None:
        config = load_initialization_config()
        self.enqueue_draft("RUNNER-PLANNER")
        self.run_ctl("schedule-preflight", "--config", str(CONFIG_PATH))
        self.add_task("RUNNER-WORKER", "project-1", capability_level="L3")
        settings = replace(
            ExecutionDispatchSettings.from_config(config), database_path=self.db_path
        )
        ExecutionDispatcher(
            settings,
            config,
            execution_id_factory=lambda _level: "runner-worker-queued",
            logger=EventLogger(None),
        ).run()

        snapshot = queue_snapshot(self.db_path, config)

        self.assertTrue(snapshot["launch_enabled"])
        self.assertEqual(snapshot["planner"]["queued"], 1)
        self.assertEqual(
            snapshot["planner"]["available_slots"],
            int(config["planner"]["max_active_executions"]) - 1,
        )
        self.assertEqual(
            snapshot["planner"]["launch_slots"],
            int(config["planner"]["max_active_executions"]),
        )
        self.assertEqual(snapshot["worker"]["queued"], 1)
        self.assertEqual(
            snapshot["planner"]["candidates"][0]["execution_kind"], "PLANNER"
        )
        self.assertEqual(
            snapshot["worker"]["candidates"][0]["execution_kind"], "WORKER"
        )

    def test_schedule_atomically_queues_only_configured_capacity(self) -> None:
        for task_id in ("QUEUE-A", "QUEUE-B", "QUEUE-C"):
            self.enqueue_draft(task_id)

        result = self.run_ctl("schedule-preflight", "--config", str(CONFIG_PATH))
        capacity = min(3, int(load_initialization_config()["planner"]["max_active_executions"]))

        self.assertEqual(result["outcome"], "QUEUED")
        self.assertEqual(result["queued_count"], capacity)
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
            ["QUEUED"] * capacity + ["UNINSPECTED"] * (3 - capacity),
        )
        self.assertEqual([row["status"] for row in executions], ["QUEUED"] * capacity)
        self.assertEqual(
            {row["preflight_execution_id"] for row in states[:capacity]},
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

    def test_unschedule_preflight_returns_unclaimed_task_to_draft(self) -> None:
        self.enqueue_draft("PLANNER-WITHDRAW")
        scheduled = self.run_ctl(
            "schedule-preflight", "--config", str(CONFIG_PATH)
        )
        execution_id = next(
            item["execution_id"]
            for item in scheduled["queued"]
            if item["task_id"] == "PLANNER-WITHDRAW"
        )

        result = self.run_ctl(
            "unschedule-preflight",
            "PLANNER-WITHDRAW",
            "--expected-row-version",
            "2",
            "--reason",
            "测试撤回 Planner 排队。",
        )

        database = connect(self.db_path)
        task = database.execute(
            "SELECT status, preflight_status, preflight_execution_id, row_version "
            "FROM tasks WHERE id='PLANNER-WITHDRAW'"
        ).fetchone()
        execution = database.execute(
            "SELECT status, outcome, finished_at, termination_reason "
            "FROM preflight_executions WHERE execution_id=?",
            (execution_id,),
        ).fetchone()
        validation = validate_database(database)
        database.close()
        self.assertEqual(result["outcome"], "PREFLIGHT_UNSCHEDULED")
        self.assertEqual(tuple(task), ("DRAFT", "UNINSPECTED", None, 3))
        self.assertEqual(execution["status"], "FINISHED")
        self.assertIsNone(execution["outcome"])
        self.assertIsNotNone(execution["finished_at"])
        self.assertEqual(execution["termination_reason"], "测试撤回 Planner 排队。")
        self.assertTrue(validation["ok"], validation["errors"])

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
        self.assertEqual(result["event"], "scheduler.preflight_schedule.completed")
        self.assertIn(str(REPOSITORY_ROOT / "control" / "loopctl.py"), command)
        self.assertIn("schedule-preflight", command)

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
            "codex_cli",
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
            "codex_cli",
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
            "codex_cli",
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
