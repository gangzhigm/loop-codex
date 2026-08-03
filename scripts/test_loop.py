from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True

from loopdb import (
    ALLOWED_TABLES,
    ARCHIVABLE_STATUSES,
    DEFAULT_DB,
    EXECUTION_PROFILES,
    RUNTIME_ENVIRONMENTS,
    SCHEMA_PATH,
    SCHEMA_USER_VERSION,
    all_tasks,
    connect,
    initialize_schema,
    insert_task,
    load_initialization_config,
    now_shanghai,
)


BASE_DIR = Path(__file__).resolve().parent.parent
LOOPCTL = BASE_DIR / "scripts" / "loopctl.py"


class LoopConcurrencyTests(unittest.TestCase):
    def test_initialization_config_owns_deployment_settings(self) -> None:
        config = load_initialization_config()
        self.assertEqual(config["automations"]["worker_interval_minutes"], 20)
        self.assertEqual(config["prompts"]["operator"], "prompts/operator.md")
        self.assertEqual(config["prompts"]["worker"], "prompts/worker.md")
        self.assertNotIn("health_interval_minutes", config["automations"])
        self.assertEqual(config["health"]["scheduler"], "windows_task_scheduler")
        self.assertEqual(config["health"]["interval_minutes"], 30)
        self.assertEqual(config["dashboard"]["port"], 4178)
        self.assertEqual(config["health"]["failure_threshold"], 3)
        self.assertEqual(config["priority_policy"]["levels"], ["blocker", "critical", "high", "medium", "low"])
        self.assertEqual(set(config["automations"]["profiles"]), set(EXECUTION_PROFILES))
        self.assertEqual(set(config["runtime_environments"]), set(RUNTIME_ENVIRONMENTS))
        self.assertEqual(config["automations"]["runtime_environment"], "codex_automation")
        self.assertEqual(
            config["task_execution"]["profile_parallel_limits"],
            {"routine": 2, "standard": 3, "advanced": 2, "deep": 1, "complex": 1, "exceptional": 1},
        )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "test.sqlite3"
        database = connect(self.db_path)
        initialize_schema(database)
        database.close()
        self.project_paths = [f"project-{index}" for index in range(1, 9)]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def add_task(
        self,
        task_id: str,
        project: str,
        priority: str = "medium",
        execution_profile: str = "standard",
        runtime_environment: str = "codex_automation",
    ) -> None:
        database = connect(self.db_path)
        insert_task(
            database,
            {
                "id": task_id,
                "title": task_id,
                "description": "test",
                "status": "PENDING",
                "priority": priority,
                "execution_profile": execution_profile,
                "runtime_environment": runtime_environment,
                "created_at": now_shanghai(),
                "scope": [f"{project}/file.txt"],
                "acceptance": ["test"],
            },
            actor="test",
            project_paths=self.project_paths,
        )
        database.close()

    def run_ctl(
        self,
        *arguments: str,
        input_text: str | None = None,
        db_path: Path | None = None,
    ) -> dict[str, object]:
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            [sys.executable, str(LOOPCTL), "--db", str(db_path or self.db_path), *arguments],
            cwd=BASE_DIR,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
            input=input_text,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def claim(
        self,
        execution_id: str,
        profile: str = "standard",
        runtime_environment: str = "codex_automation",
    ) -> dict[str, object]:
        return self.run_ctl(
            "claim",
            execution_id,
            "--profile",
            profile,
            "--runtime-environment",
            runtime_environment,
        )

    def run_ctl_error(self, *arguments: str, db_path: Path | None = None) -> dict[str, object]:
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            [sys.executable, str(LOOPCTL), "--db", str(db_path or self.db_path), *arguments],
            cwd=BASE_DIR,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
        )
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        return json.loads(completed.stdout)

    def finish(self, execution_id: str, task_id: str) -> dict[str, object]:
        return self.run_ctl(
            "finish",
            execution_id,
            task_id,
            input_text=json.dumps({"status": "SUCCEEDED", "summary": "done", "verification": ["ok"]}),
        )

    def test_database_contains_only_task_tables(self) -> None:
        database = connect(self.db_path)
        tables = {
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        database.close()
        self.assertEqual(tables, ALLOWED_TABLES)
        self.assertEqual(DEFAULT_DB, BASE_DIR / "data" / "loop-agent.sqlite3")

    def test_fresh_schema_has_routing_fields_and_current_version(self) -> None:
        database = connect(self.db_path)
        columns = {row[1] for row in database.execute("PRAGMA table_info(tasks)").fetchall()}
        version = database.execute("PRAGMA user_version").fetchone()[0]
        database.close()
        self.assertIn("archived_at", columns)
        self.assertIn("execution_profile", columns)
        self.assertIn("runtime_environment", columns)
        self.assertEqual(version, SCHEMA_USER_VERSION)

    def test_six_parallel_claims_and_seventh_slot_full(self) -> None:
        profiles = ["routine", "routine", "standard", "standard", "advanced", "deep", "complex"]
        for index, profile in enumerate(profiles, start=1):
            self.add_task(f"TASK-{index}", f"project-{index}", execution_profile=profile)
        with ThreadPoolExecutor(max_workers=7) as pool:
            results = list(
                pool.map(
                    lambda item: self.claim(f"exec-{item[0]}", item[1]),
                    enumerate(profiles, start=1),
                )
            )
        outcomes = [result["outcome"] for result in results]
        self.assertEqual(outcomes.count("CLAIMED"), 6)
        self.assertEqual(outcomes.count("SLOT_FULL"), 1)
        full = next(result for result in results if result["outcome"] == "SLOT_FULL")
        self.assertEqual(full["limit_scope"], "global")

    def test_profile_parallel_limit_is_enforced(self) -> None:
        for index in range(1, 4):
            self.add_task(f"ROUTINE-{index}", f"project-{index}", execution_profile="routine")
        self.assertEqual(self.claim("routine-1", "routine")["outcome"], "CLAIMED")
        self.assertEqual(self.claim("routine-2", "routine")["outcome"], "CLAIMED")
        full = self.claim("routine-3", "routine")
        self.assertEqual(full["outcome"], "SLOT_FULL")
        self.assertEqual(full["limit_scope"], "profile")
        self.assertEqual(full["profile_active"], 2)
        self.assertEqual(full["profile_maximum"], 2)

    def test_claim_isolated_by_execution_profile(self) -> None:
        self.add_task("STANDARD-BLOCKER", "project-1", "blocker", "standard")
        self.add_task("ROUTINE-LOW", "project-2", "low", "routine")
        result = self.claim("routine-only", "routine")
        self.assertEqual(result["task"]["id"], "ROUTINE-LOW")
        self.assertEqual(result["task"]["execution_profile"], "routine")

    def test_claim_isolated_by_runtime_environment(self) -> None:
        self.add_task("AUTOMATION-BLOCKER", "project-1", "blocker", runtime_environment="codex_automation")
        self.add_task("CLI-LOW", "project-2", "low", runtime_environment="codex_cli")
        result = self.claim("cli-only", runtime_environment="codex_cli")
        self.assertEqual(result["task"]["id"], "CLI-LOW")
        self.assertEqual(result["task"]["runtime_environment"], "codex_cli")
        self.assertEqual(result["runtime_environment"], "codex_cli")
        state = self.run_ctl("state")
        agent = next(item for item in state["agents"] if item["id"] == "cli-only")
        self.assertEqual(agent["runtime_environment"], "codex_cli")

    def test_claim_requires_explicit_runtime_environment(self) -> None:
        self.add_task("EXPLICIT-ENV", "project-1")
        completed = subprocess.run(
            [sys.executable, str(LOOPCTL), "--db", str(self.db_path), "claim", "missing-env", "--profile", "standard"],
            cwd=BASE_DIR,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        self.assertNotEqual(completed.returncode, 0)
        database = connect(self.db_path)
        status = database.execute("SELECT status FROM tasks WHERE id='EXPLICIT-ENV'").fetchone()[0]
        database.close()
        self.assertEqual(status, "PENDING")

    def test_profile_limit_is_shared_across_runtime_environments(self) -> None:
        environments = ["codex_automation", "codex_cli", "deepseek"]
        for index, environment in enumerate(environments, start=1):
            self.add_task(f"ENV-{index}", f"project-{index}", runtime_environment=environment)
        self.add_task("ENV-4", "project-4", runtime_environment="codex_automation")
        for index, environment in enumerate(environments, start=1):
            self.assertEqual(self.claim(f"env-exec-{index}", runtime_environment=environment)["outcome"], "CLAIMED")
        full = self.claim("env-exec-4", runtime_environment="codex_automation")
        self.assertEqual(full["outcome"], "SLOT_FULL")
        self.assertEqual(full["limit_scope"], "profile")

    def test_scope_conflict_is_shared_across_runtime_environments(self) -> None:
        self.add_task("AUTOMATION-LOCK", "project-1", "critical", runtime_environment="codex_automation")
        self.add_task("CLI-CONFLICT", "project-1", "high", runtime_environment="codex_cli")
        self.assertEqual(self.claim("automation-lock")["outcome"], "CLAIMED")
        conflict = self.claim("cli-conflict", runtime_environment="codex_cli")
        self.assertEqual(conflict["outcome"], "CONFLICT")
        self.assertEqual(conflict["task_id"], "CLI-CONFLICT")

    def test_blocker_is_first_priority_within_profile(self) -> None:
        self.add_task("OLDER-CRITICAL", "project-1", "critical")
        self.add_task("NEWER-BLOCKER", "project-2", "blocker")
        result = self.claim("priority-order")
        self.assertEqual(result["task"]["id"], "NEWER-BLOCKER")

    def test_conflict_waits_then_requeues_after_blocker_finishes(self) -> None:
        self.add_task("BLOCKER", "project-1", "critical")
        self.add_task("CONFLICT", "project-1", "high")
        first = self.claim("exec-blocker")
        self.assertEqual(first["task"]["id"], "BLOCKER")
        second = self.claim("exec-conflict")
        self.assertEqual(second["outcome"], "CONFLICT")
        self.assertEqual(second["task_id"], "CONFLICT")
        finished = self.finish("exec-blocker", "BLOCKER")
        self.assertIn("CONFLICT", finished["requeued_conflicts"])
        database = connect(self.db_path)
        status = database.execute("SELECT status FROM tasks WHERE id='CONFLICT'").fetchone()[0]
        database.close()
        self.assertEqual(status, "PENDING")

    def test_conflicting_candidate_does_not_hide_runnable_task(self) -> None:
        self.add_task("BLOCKER", "project-1", "critical")
        self.add_task("CONFLICT", "project-1", "high")
        self.add_task("RUNNABLE", "project-2", "medium")
        self.claim("exec-blocker")

        result = self.claim("exec-runnable")

        self.assertEqual(result["outcome"], "CLAIMED")
        self.assertEqual(result["task"]["id"], "RUNNABLE")
        self.assertEqual(result["deferred_conflicts"][0]["task_id"], "CONFLICT")
        database = connect(self.db_path)
        conflict_status = database.execute(
            "SELECT status FROM tasks WHERE id='CONFLICT'"
        ).fetchone()[0]
        database.close()
        self.assertEqual(conflict_status, "WAITING_CONFLICT")

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
                "runtime_environment": "codex_automation",
                "created_at": now_shanghai(),
                "scope": ["project-1/a.txt", "project-1/b.txt", "project-1/sub/c.txt"],
                "acceptance": ["test"],
            },
            actor="test",
            project_paths=self.project_paths,
        )
        database.close()
        result = self.claim("exec-multi")
        self.assertEqual(result["outcome"], "CLAIMED")
        database = connect(self.db_path)
        lock_count = database.execute(
            "SELECT count(*) FROM scope_locks WHERE execution_id='exec-multi'"
        ).fetchone()[0]
        database.close()
        self.assertEqual(lock_count, 1)

    def test_expired_lease_is_recovered_and_reclaimed(self) -> None:
        self.add_task("LEASE", "project-1")
        self.claim("exec-old")
        database = connect(self.db_path)
        database.execute("UPDATE executions SET lease_expires_at='2000-01-01T00:00:00+08:00' WHERE execution_id='exec-old'")
        database.execute("UPDATE scope_locks SET lease_expires_at='2000-01-01T00:00:00+08:00' WHERE execution_id='exec-old'")
        database.close()
        result = self.claim("exec-new")
        self.assertEqual(result["outcome"], "CLAIMED")
        self.assertEqual(result["task"]["id"], "LEASE")
        self.assertEqual(result["task"]["attempt"], 2)

    def test_stalled_heartbeat_is_recovered_before_lease_expiry(self) -> None:
        self.add_task("STALLED", "project-1")
        self.claim("exec-stalled")
        database = connect(self.db_path)
        database.execute(
            "UPDATE executions SET heartbeat_at='2000-01-01T00:00:00+08:00', "
            "lease_expires_at='2999-01-01T00:00:00+08:00' WHERE execution_id='exec-stalled'"
        )
        database.execute(
            "UPDATE tasks SET heartbeat_at='2000-01-01T00:00:00+08:00' WHERE id='STALLED'"
        )
        database.close()

        result = self.claim("exec-recovered")

        self.assertEqual(result["outcome"], "CLAIMED")
        self.assertEqual(result["task"]["id"], "STALLED")
        self.assertEqual(result["recovered"][0]["outcome"], "HEARTBEAT_STALLED")
        self.assertEqual(result["task"]["attempt"], 2)

    def test_two_conflicts_and_stalled_blocker_do_not_deadlock_new_claim(self) -> None:
        self.add_task("BLOCKER", "project-1", "critical")
        self.add_task("CONFLICT-1", "project-1", "high")
        self.add_task("CONFLICT-2", "project-1", "high")
        self.claim("exec-blocker")
        conflict_result = self.claim("exec-conflicts")
        self.assertEqual(conflict_result["outcome"], "CONFLICT")
        self.assertEqual(
            [item["task_id"] for item in conflict_result["deferred_conflicts"]],
            ["CONFLICT-1", "CONFLICT-2"],
        )
        self.add_task("NEW-RUNNABLE", "project-2", "medium")
        database = connect(self.db_path)
        database.execute(
            "UPDATE executions SET heartbeat_at='2000-01-01T00:00:00+08:00', "
            "lease_expires_at='2999-01-01T00:00:00+08:00' WHERE execution_id='exec-blocker'"
        )
        database.execute("UPDATE tasks SET attempt=2 WHERE id='BLOCKER'")
        database.close()

        result = self.claim("exec-next")

        self.assertEqual(result["outcome"], "CLAIMED")
        self.assertEqual(result["recovered"][0]["outcome"], "HEARTBEAT_STALLED")
        self.assertIn(result["task"]["id"], {"CONFLICT-1", "CONFLICT-2", "NEW-RUNNABLE"})
        database = connect(self.db_path)
        statuses = dict(
            database.execute(
                "SELECT id, status FROM tasks WHERE id IN ('CONFLICT-1', 'CONFLICT-2', 'NEW-RUNNABLE')"
            ).fetchall()
        )
        database.close()
        self.assertIn("RUNNING", statuses.values())
        self.assertNotIn("WAITING_CONFLICT", statuses.values())

    def test_succeeded_requires_manual_confirmation(self) -> None:
        self.add_task("CONFIRM", "project-1")
        self.claim("exec-confirm")
        self.finish("exec-confirm", "CONFIRM")
        result = self.run_ctl("confirm", "CONFIRM", "--reason", "人工复核通过")
        self.assertEqual(result["outcome"], "CONFIRMED")
        database = connect(self.db_path)
        history = database.execute(
            "SELECT from_status, to_status FROM task_history WHERE task_id='CONFIRM' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        archived_at = database.execute(
            "SELECT archived_at FROM tasks WHERE id='CONFIRM'"
        ).fetchone()[0]
        payload = next(task for task in all_tasks(database) if task["id"] == "CONFIRM")
        database.close()
        self.assertEqual(tuple(history), ("SUCCEEDED", "CONFIRMED"))
        self.assertIsNone(archived_at)
        self.assertIn("archived_at", payload)
        self.assertIsNone(payload["archived_at"])

    def test_archive_and_unarchive_are_idempotent_and_preserve_task_data(self) -> None:
        for status in sorted(ARCHIVABLE_STATUSES):
            task_id = f"ARCHIVE-{status}"
            self.add_task(task_id, "project-1")
            database = connect(self.db_path)
            database.execute(
                "UPDATE tasks SET status=?, attempt=2, result_summary='kept', result_error='kept-error' "
                "WHERE id=?",
                (status, task_id),
            )
            database.close()
            result = self.run_ctl("archive", task_id, "--reason", f"archive {status}")
            self.assertEqual(result["outcome"], "ARCHIVED")
            self.assertEqual(result["status"], status)
            self.assertIsNotNone(result["archived_at"])

        task_id = "ARCHIVE-FAILED"
        database = connect(self.db_path)
        before = database.execute(
            "SELECT status, attempt, result_summary, result_error FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        history_before_repeat = database.execute(
            "SELECT count(*) FROM task_history WHERE task_id=?", (task_id,)
        ).fetchone()[0]
        archived_at = database.execute(
            "SELECT archived_at FROM tasks WHERE id=?", (task_id,)
        ).fetchone()[0]
        database.close()
        self.assertIsNotNone(datetime.fromisoformat(archived_at).utcoffset())

        repeated = self.run_ctl("archive", task_id, "--reason", "must not duplicate")
        self.assertEqual(repeated["outcome"], "ALREADY_ARCHIVED")
        database = connect(self.db_path)
        self.assertEqual(
            database.execute("SELECT count(*) FROM task_history WHERE task_id=?", (task_id,)).fetchone()[0],
            history_before_repeat,
        )
        unarchived = self.run_ctl("unarchive", task_id, "--reason", "return to current view")
        self.assertEqual(unarchived["outcome"], "UNARCHIVED")
        history_after_unarchive = database.execute(
            "SELECT count(*) FROM task_history WHERE task_id=?", (task_id,)
        ).fetchone()[0]
        database.close()
        repeated_unarchive = self.run_ctl("unarchive", task_id, "--reason", "must not duplicate")
        self.assertEqual(repeated_unarchive["outcome"], "ALREADY_UNARCHIVED")

        database = connect(self.db_path)
        after = database.execute(
            "SELECT status, attempt, result_summary, result_error FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        final_history = database.execute(
            "SELECT count(*) FROM task_history WHERE task_id=?", (task_id,)
        ).fetchone()[0]
        archive_events = database.execute(
            "SELECT from_status, to_status, actor, reason FROM task_history "
            "WHERE task_id=? AND actor='task-manager' ORDER BY id",
            (task_id,),
        ).fetchall()
        database.close()
        self.assertEqual(tuple(after), tuple(before))
        self.assertEqual(final_history, history_after_unarchive)
        self.assertEqual(
            [(row["from_status"], row["to_status"]) for row in archive_events],
            [("FAILED", "FAILED"), ("FAILED", "FAILED")],
        )

    def test_archive_rejects_nonterminal_statuses(self) -> None:
        statuses = ["DRAFT", "PENDING", "RUNNING", "WAITING_CONFLICT", "WAITING_HUMAN", "SUCCEEDED"]
        for index, status in enumerate(statuses):
            task_id = f"NOT-ARCHIVABLE-{index}"
            self.add_task(task_id, "project-1")
            database = connect(self.db_path)
            database.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
            database.close()
            with self.subTest(status=status):
                result = self.run_ctl_error("archive", task_id, "--reason", "must fail")
                self.assertEqual(result["outcome"], "ERROR")
                self.assertIn("只有终态任务可以归档", result["message"])

    def test_migrate_legacy_confirmed_tasks_only(self) -> None:
        legacy_path = Path(self.temporary.name) / "legacy.sqlite3"
        legacy_schema = self.legacy_schema(30000)
        legacy_schema = legacy_schema.replace("  archived_at TEXT,\n", "")
        legacy_schema = legacy_schema.replace(
            "\nCREATE INDEX IF NOT EXISTS idx_tasks_archived\n  ON tasks(archived_at, status, updated_at);\n",
            "\n",
        )
        database = connect(legacy_path)
        database.executescript(legacy_schema)
        stamp = "2026-07-30T12:34:56.000+08:00"
        database.executemany(
            "INSERT INTO tasks(id, title, description, status, priority, created_at, updated_at, "
            "completed_at, progress_summary) VALUES(?, ?, '', ?, 'medium', ?, ?, ?, '')",
            [
                ("OLD-CONFIRMED", "old confirmed", "CONFIRMED", stamp, stamp, stamp),
                ("OLD-CANCELLED", "old cancelled", "CANCELLED", stamp, stamp, stamp),
            ],
        )
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES('OLD-CONFIRMED', ?, 'SUCCEEDED', 'CONFIRMED', 'human-review', 'legacy confirm')",
            (stamp,),
        )
        database.close()

        migrated = self.run_ctl("migrate", db_path=legacy_path)
        self.assertEqual(migrated["outcome"], "MIGRATED")
        self.assertEqual(migrated["archived"], 1)
        self.assertEqual(migrated["profiles_backfilled"], 2)
        self.assertEqual(migrated["runtime_environments_backfilled"], 2)
        database = connect(legacy_path)
        rows = dict(database.execute("SELECT id, archived_at FROM tasks ORDER BY id").fetchall())
        migration_events = database.execute(
            "SELECT count(*) FROM task_history WHERE actor='schema-migration'"
        ).fetchone()[0]
        database.close()
        self.assertEqual(rows["OLD-CONFIRMED"], stamp)
        self.assertIsNone(rows["OLD-CANCELLED"])
        self.assertEqual(migration_events, 1)

        repeated = self.run_ctl("migrate", db_path=legacy_path)
        self.assertEqual(repeated["outcome"], "ALREADY_CURRENT")
        database = connect(legacy_path)
        self.assertEqual(
            database.execute("SELECT count(*) FROM task_history WHERE actor='schema-migration'").fetchone()[0],
            migration_events,
        )
        database.close()

    def test_migrate_schema_31_backfills_standard_profile(self) -> None:
        legacy_path = Path(self.temporary.name) / "schema-31.sqlite3"
        database = connect(legacy_path)
        database.executescript(self.legacy_schema(30100))
        stamp = "2026-07-30T12:34:56.000+08:00"
        database.execute(
            "INSERT INTO tasks(id, title, description, status, priority, created_at, updated_at) "
            "VALUES('SCHEMA-31', 'schema 31', '', 'PENDING', 'high', ?, ?)",
            (stamp, stamp),
        )
        database.close()

        migrated = self.run_ctl("migrate", db_path=legacy_path)
        self.assertEqual(migrated["from"], "3.1.0")
        self.assertEqual(migrated["to"], "3.3.0")
        self.assertEqual(migrated["archived"], 0)
        self.assertEqual(migrated["profiles_backfilled"], 1)
        database = connect(legacy_path)
        row = database.execute(
            "SELECT priority, execution_profile, runtime_environment FROM tasks WHERE id='SCHEMA-31'"
        ).fetchone()
        database.close()
        self.assertEqual(tuple(row), ("high", "standard", "codex_automation"))

    def test_update_profile_is_exposed_by_state(self) -> None:
        self.add_task("PROFILE-UPDATE", "project-1")
        patch_path = Path(self.temporary.name) / "profile-patch.json"
        patch_path.write_text('{"execution_profile":"advanced"}', encoding="utf-8")
        self.run_ctl("update", "PROFILE-UPDATE", str(patch_path))
        state = self.run_ctl("state")
        task = next(item for item in state["tasks"] if item["id"] == "PROFILE-UPDATE")
        self.assertEqual(task["execution_profile"], "advanced")

    def test_enqueue_and_update_runtime_environment_are_exposed_by_state(self) -> None:
        task_path = Path(self.temporary.name) / "runtime-task.json"
        task_path.write_text(
            json.dumps(
                {
                    "id": "RUNTIME-UPDATE",
                    "title": "runtime update",
                    "description": "test",
                    "priority": "medium",
                    "execution_profile": "standard",
                    "runtime_environment": "codex_cli",
                    "scope": ["local-agent-loop/scripts/loopctl.py"],
                    "acceptance": ["test"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.run_ctl("enqueue", str(task_path))
        patch_path = Path(self.temporary.name) / "runtime-patch.json"
        patch_path.write_text('{"runtime_environment":"deepseek"}', encoding="utf-8")
        self.run_ctl("update", "RUNTIME-UPDATE", str(patch_path))
        state = self.run_ctl("state")
        task = next(item for item in state["tasks"] if item["id"] == "RUNTIME-UPDATE")
        self.assertEqual(task["runtime_environment"], "deepseek")

        patch_path.write_text('{"runtime_environment":"unknown"}', encoding="utf-8")
        error = self.run_ctl_error("update", "RUNTIME-UPDATE", str(patch_path))
        self.assertIn("运行环境无效", error["message"])

    def test_enqueue_requires_runtime_environment(self) -> None:
        task_path = Path(self.temporary.name) / "missing-runtime-task.json"
        task_path.write_text(
            json.dumps(
                {
                    "id": "MISSING-RUNTIME",
                    "title": "missing runtime",
                    "scope": ["project-1/file.txt"],
                }
            ),
            encoding="utf-8",
        )
        error = self.run_ctl_error("enqueue", str(task_path))
        self.assertIn("运行环境无效", error["message"])

    def test_migrate_schema_32_preserves_task_data_and_execution_history(self) -> None:
        schema_32_path = Path(self.temporary.name) / "schema-32.sqlite3"
        database = connect(schema_32_path)
        database.executescript(self.schema_32())
        stamp = "2026-08-03T09:00:00.000+08:00"
        database.executemany(
            "INSERT INTO tasks(id, title, description, status, priority, execution_profile, assigned_agent, "
            "created_at, started_at, updated_at, heartbeat_at, completed_at, archived_at, attempt, "
            "progress_percent, progress_summary, progress_next_step, result_summary, result_error, "
            "human_required, human_question, human_options_json, human_requested_at, human_responded_at, "
            "human_response, row_version) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "SCHEMA-32-ROOT", "root", "root task", "SUCCEEDED", "high", "advanced", None,
                    stamp, stamp, stamp, stamp, stamp, None, 1, 100, "done", None, "root result", None,
                    0, None, "[]", None, None, None, 3,
                ),
                (
                    "SCHEMA-32-ROUTED", "routed", "routed task", "FAILED", "blocker", "deep", "old-worker",
                    stamp, stamp, stamp, stamp, stamp, stamp, 2, 100, "failed", None, None, "kept error",
                    0, None, "[]", None, None, None, 17,
                ),
            ],
        )
        database.execute(
            "INSERT INTO task_dependencies(task_id, dependency_id) VALUES('SCHEMA-32-ROUTED', 'SCHEMA-32-ROOT')"
        )
        database.execute(
            "INSERT INTO task_scopes(task_id, ordinal, scope, scope_key) "
            "VALUES('SCHEMA-32-ROUTED', 0, 'local-agent-loop/scripts/loopdb.py', 'project:local-agent-loop')"
        )
        database.execute(
            "INSERT INTO task_acceptance(task_id, ordinal, text) VALUES('SCHEMA-32-ROUTED', 0, 'kept acceptance')"
        )
        database.execute(
            "INSERT INTO task_completed_items(task_id, ordinal, text) VALUES('SCHEMA-32-ROUTED', 0, 'kept completed')"
        )
        database.execute(
            "INSERT INTO task_verifications(task_id, ordinal, text) VALUES('SCHEMA-32-ROUTED', 0, 'kept verification')"
        )
        database.execute(
            "INSERT INTO task_attachments(task_id, ordinal, path, sha256, role, saved_at) "
            "VALUES('SCHEMA-32-ROUTED', 0, 'assets/file.txt', 'abc', 'result', ?)",
            (stamp,),
        )
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES('SCHEMA-32-ROUTED', ?, 'RUNNING', 'FAILED', 'old-worker', 'kept history')",
            (stamp,),
        )
        database.execute(
            "INSERT INTO executions(execution_id, task_id, status, started_at, heartbeat_at, lease_expires_at, "
            "finished_at, outcome) VALUES('old-execution', 'SCHEMA-32-ROUTED', 'FINISHED', ?, ?, ?, ?, 'FAILED')",
            (stamp, stamp, stamp, stamp),
        )
        child_tables = [
            "task_dependencies", "task_scopes", "task_acceptance", "task_completed_items",
            "task_verifications", "task_attachments", "task_history", "executions",
        ]
        tasks_before = {
            row["id"]: dict(row) for row in database.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        }
        children_before = {
            table: [tuple(row) for row in database.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]
            for table in child_tables
        }
        database.close()

        migrated = self.run_ctl("migrate", db_path=schema_32_path)
        self.assertEqual(migrated["from"], "3.2.0")
        self.assertEqual(migrated["to"], "3.3.0")
        self.assertEqual(migrated["profiles_backfilled"], 0)
        self.assertEqual(migrated["runtime_environments_backfilled"], 2)

        database = connect(schema_32_path)
        tasks_after = {
            row["id"]: dict(row) for row in database.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        }
        for task_id, before in tasks_before.items():
            after = tasks_after[task_id]
            self.assertEqual(after.pop("runtime_environment"), "codex_automation")
            self.assertEqual(after, before)
        children_after = {
            table: [tuple(row) for row in database.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]
            for table in child_tables
        }
        database.close()
        self.assertEqual(children_after, children_before)

        repeated = self.run_ctl("migrate", db_path=schema_32_path)
        self.assertEqual(repeated["outcome"], "ALREADY_CURRENT")

    def test_migrate_schema_32_rejects_active_execution(self) -> None:
        schema_32_path = Path(self.temporary.name) / "schema-32-active.sqlite3"
        database = connect(schema_32_path)
        database.executescript(self.schema_32())
        stamp = "2026-08-03T09:00:00.000+08:00"
        database.execute(
            "INSERT INTO tasks(id, title, status, priority, execution_profile, created_at, updated_at) "
            "VALUES('ACTIVE-32', 'active', 'RUNNING', 'critical', 'complex', ?, ?)",
            (stamp, stamp),
        )
        database.execute(
            "INSERT INTO executions(execution_id, task_id, status, started_at, heartbeat_at, lease_expires_at) "
            "VALUES('active-execution', 'ACTIVE-32', 'RUNNING', ?, ?, ?)",
            (stamp, stamp, "2999-01-01T00:00:00.000+08:00"),
        )
        database.close()
        error = self.run_ctl_error("migrate", db_path=schema_32_path)
        self.assertIn("Schema 迁移要求没有活动 execution", error["message"])

    def test_update_rejects_dependency_cycle_with_full_path(self) -> None:
        for index in range(1, 4):
            self.add_task(f"CYCLE-{index}", f"project-{index}")
        patch_path = Path(self.temporary.name) / "dependency-patch.json"
        for task_id, dependency_id in (("CYCLE-1", "CYCLE-2"), ("CYCLE-2", "CYCLE-3")):
            patch_path.write_text(
                json.dumps({"depends_on": [dependency_id]}, ensure_ascii=False), encoding="utf-8"
            )
            self.run_ctl("update", task_id, str(patch_path))
        patch_path.write_text('{"depends_on":["CYCLE-1"]}', encoding="utf-8")
        error = self.run_ctl_error("update", "CYCLE-3", str(patch_path))
        self.assertEqual(error["outcome"], "ERROR")
        self.assertIn("CYCLE-1 -> CYCLE-2 -> CYCLE-3 -> CYCLE-1", error["message"])

    def test_succeeded_can_be_reopened_by_operator(self) -> None:
        self.add_task("REOPEN", "project-1")
        self.claim("exec-reopen")
        self.finish("exec-reopen", "REOPEN")
        result = self.run_ctl("requeue", "REOPEN", "--reason", "人工要求重新执行")
        self.assertEqual(result["outcome"], "REQUEUED")
        database = connect(self.db_path)
        history = database.execute(
            "SELECT from_status, to_status FROM task_history WHERE task_id='REOPEN' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        database.close()
        self.assertEqual(tuple(history), ("SUCCEEDED", "PENDING"))

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

    @staticmethod
    def legacy_schema(user_version: int) -> str:
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        schema = schema.replace("PRAGMA user_version = 30300;", f"PRAGMA user_version = {user_version};")
        schema = schema.replace(
            "  priority TEXT NOT NULL CHECK (priority IN ('blocker', 'critical', 'high', 'medium', 'low')),\n",
            "  priority TEXT NOT NULL CHECK (priority IN ('critical', 'high', 'medium', 'low')),\n",
        )
        schema = schema.replace(
            "  execution_profile TEXT NOT NULL DEFAULT 'standard' CHECK (execution_profile IN (\n"
            "    'routine', 'standard', 'advanced', 'deep', 'complex', 'exceptional'\n"
            "  )),\n",
            "",
        )
        schema = schema.replace(
            "  runtime_environment TEXT NOT NULL CHECK (runtime_environment IN (\n"
            "    'codex_automation', 'codex_cli', 'deepseek'\n"
            "  )),\n",
            "",
        )
        schema = schema.replace(
            "CREATE INDEX IF NOT EXISTS idx_tasks_queue\n"
            "  ON tasks(status, runtime_environment, execution_profile, priority, created_at, id);",
            "CREATE INDEX IF NOT EXISTS idx_tasks_queue\n"
            "  ON tasks(status, priority, created_at, id);",
        )
        return schema

    @staticmethod
    def schema_32() -> str:
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        schema = schema.replace("PRAGMA user_version = 30300;", "PRAGMA user_version = 30200;")
        schema = schema.replace(
            "  runtime_environment TEXT NOT NULL CHECK (runtime_environment IN (\n"
            "    'codex_automation', 'codex_cli', 'deepseek'\n"
            "  )),\n",
            "",
        )
        schema = schema.replace(
            "CREATE INDEX IF NOT EXISTS idx_tasks_queue\n"
            "  ON tasks(status, runtime_environment, execution_profile, priority, created_at, id);",
            "CREATE INDEX IF NOT EXISTS idx_tasks_queue\n"
            "  ON tasks(status, execution_profile, priority, created_at, id);",
        )
        return schema


if __name__ == "__main__":
    unittest.main()
