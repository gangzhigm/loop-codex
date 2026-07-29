from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.dont_write_bytecode = True

from loopdb import connect, initialize_schema, insert_task, load_initialization_config, now_shanghai, set_setting


BASE_DIR = Path(__file__).resolve().parent.parent
LOOPCTL = BASE_DIR / "scripts" / "loopctl.py"


class LoopConcurrencyTests(unittest.TestCase):
    def test_initialization_config_owns_deployment_settings(self) -> None:
        config = load_initialization_config()
        self.assertEqual(config["automations"]["worker_interval_minutes"], 10)
        self.assertEqual(config["automations"]["health_interval_minutes"], 30)
        self.assertEqual(config["dashboard"]["port"], 4178)
        self.assertEqual(config["health"]["failure_threshold"], 3)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "test.sqlite3"
        database = connect(self.db_path)
        initialize_schema(database)
        set_setting(database, "max_parallel_tasks", 6)
        set_setting(database, "task_lease_seconds", 3600)
        set_setting(database, "max_attempts", 2)
        stamp = now_shanghai()
        for index in range(1, 9):
            database.execute(
                "INSERT INTO projects(path, description, exists_on_disk, updated_at) VALUES(?, '', 1, ?)",
                (f"project-{index}", stamp),
            )
        database.close()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def add_task(self, task_id: str, project: str, priority: str = "medium") -> None:
        database = connect(self.db_path)
        insert_task(
            database,
            {
                "id": task_id,
                "title": task_id,
                "description": "test",
                "status": "PENDING",
                "priority": priority,
                "created_at": now_shanghai(),
                "scope": [f"{project}/file.txt"],
                "acceptance": ["test"],
            },
            actor="test",
        )
        database.close()

    def run_ctl(self, *arguments: str) -> dict[str, object]:
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            [sys.executable, str(LOOPCTL), "--db", str(self.db_path), *arguments],
            cwd=BASE_DIR,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def finish(self, execution_id: str, task_id: str) -> dict[str, object]:
        report = Path(self.temporary.name) / f"{execution_id}.json"
        report.write_text(
            json.dumps({"status": "SUCCEEDED", "summary": "done", "verification": ["ok"]}),
            encoding="utf-8",
        )
        return self.run_ctl("finish", execution_id, task_id, str(report))

    def test_six_parallel_claims_and_seventh_slot_full(self) -> None:
        for index in range(1, 8):
            self.add_task(f"TASK-{index}", f"project-{index}")
        with ThreadPoolExecutor(max_workers=7) as pool:
            results = list(pool.map(lambda index: self.run_ctl("claim", f"exec-{index}"), range(1, 8)))
        outcomes = [result["outcome"] for result in results]
        self.assertEqual(outcomes.count("CLAIMED"), 6)
        self.assertEqual(outcomes.count("SLOT_FULL"), 1)

    def test_conflict_waits_then_requeues_after_blocker_finishes(self) -> None:
        self.add_task("BLOCKER", "project-1", "critical")
        self.add_task("CONFLICT", "project-1", "high")
        first = self.run_ctl("claim", "exec-blocker")
        self.assertEqual(first["task"]["id"], "BLOCKER")
        second = self.run_ctl("claim", "exec-conflict")
        self.assertEqual(second["outcome"], "CONFLICT")
        self.assertEqual(second["task_id"], "CONFLICT")
        finished = self.finish("exec-blocker", "BLOCKER")
        self.assertIn("CONFLICT", finished["requeued_conflicts"])
        database = connect(self.db_path)
        status = database.execute("SELECT status FROM tasks WHERE id='CONFLICT'").fetchone()[0]
        database.close()
        self.assertEqual(status, "PENDING")

    def test_multiple_files_in_one_project_acquire_one_project_lock(self) -> None:
        database = connect(self.db_path)
        insert_task(
            database,
            {
                "id": "MULTI-SCOPE",
                "title": "MULTI-SCOPE",
                "description": "test",
                "status": "PENDING",
                "priority": "critical",
                "created_at": now_shanghai(),
                "scope": ["project-1/a.txt", "project-1/b.txt", "project-1/sub/c.txt"],
                "acceptance": ["test"],
            },
            actor="test",
        )
        database.close()
        result = self.run_ctl("claim", "exec-multi")
        self.assertEqual(result["outcome"], "CLAIMED")
        database = connect(self.db_path)
        lock_count = database.execute(
            "SELECT count(*) FROM scope_locks WHERE execution_id='exec-multi'"
        ).fetchone()[0]
        database.close()
        self.assertEqual(lock_count, 1)

    def test_expired_lease_is_recovered_and_reclaimed(self) -> None:
        self.add_task("LEASE", "project-1")
        self.run_ctl("claim", "exec-old")
        database = connect(self.db_path)
        database.execute("UPDATE executions SET lease_expires_at='2000-01-01T00:00:00+08:00' WHERE execution_id='exec-old'")
        database.execute("UPDATE scope_locks SET lease_expires_at='2000-01-01T00:00:00+08:00' WHERE execution_id='exec-old'")
        database.close()
        result = self.run_ctl("claim", "exec-new")
        self.assertEqual(result["outcome"], "CLAIMED")
        self.assertEqual(result["task"]["id"], "LEASE")
        self.assertEqual(result["task"]["attempt"], 2)

    def test_succeeded_requires_manual_confirmation(self) -> None:
        self.add_task("CONFIRM", "project-1")
        self.run_ctl("claim", "exec-confirm")
        self.finish("exec-confirm", "CONFIRM")
        result = self.run_ctl("confirm", "CONFIRM", "--reason", "人工复核通过")
        self.assertEqual(result["outcome"], "CONFIRMED")
        database = connect(self.db_path)
        history = database.execute(
            "SELECT from_status, to_status FROM task_history WHERE task_id='CONFIRM' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        database.close()
        self.assertEqual(tuple(history), ("SUCCEEDED", "CONFIRMED"))

    def test_draft_can_be_requeued_after_operator_review(self) -> None:
        self.add_task("DRAFT-TASK", "project-1")
        database = connect(self.db_path)
        database.execute("UPDATE tasks SET status='DRAFT' WHERE id='DRAFT-TASK'")
        database.close()
        result = self.run_ctl("requeue", "DRAFT-TASK", "--reason", "人工需求已确认")
        self.assertEqual(result["outcome"], "REQUEUED")
        database = connect(self.db_path)
        status = database.execute("SELECT status FROM tasks WHERE id='DRAFT-TASK'").fetchone()[0]
        database.close()
        self.assertEqual(status, "PENDING")


if __name__ == "__main__":
    unittest.main()
