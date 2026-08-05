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
    CAPABILITY_LEVELS,
    CANONICAL_RUNTIME_ENVIRONMENTS,
    DEFAULT_DB,
    EXECUTION_PROFILES,
    RUNTIME_ENVIRONMENTS,
    SCHEMA_PATH,
    SCHEMA_USER_VERSION,
    LoopError,
    all_tasks,
    connect,
    initialize_schema,
    insert_task,
    load_initialization_config,
    now_shanghai,
    resolve_execution_profile,
    validate_database,
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
        self.assertEqual(set(config["runtime_environments"]), set(CANONICAL_RUNTIME_ENVIRONMENTS))
        self.assertEqual(config["automations"]["runtime_environment"], "codex_automation")
        self.assertEqual(config["task_execution"]["global_max_active_executions"], 8)
        self.assertEqual(
            config["task_execution"]["platform_max_active_executions"],
            {"codex_automation": 5, "codex_cli": 5, "self_hosted_agent": 5},
        )
        self.assertNotIn("profile_parallel_limits", config["task_execution"])
        self.assertNotIn("capability_parallel_limits", config["task_execution"])
        self.assertEqual(set(config["execution_profiles"]), set(CANONICAL_RUNTIME_ENVIRONMENTS))
        for environment in ("codex_automation", "codex_cli"):
            self.assertEqual(
                set(config["execution_profiles"][environment]["capabilities"]), set(CAPABILITY_LEVELS)
            )
        self.assertEqual(
            set(config["execution_profiles"]["self_hosted_agent"]["providers"]["deepseek"]["capabilities"]),
            set(CAPABILITY_LEVELS),
        )
        self.assertEqual(
            [
                config["execution_profiles"]["codex_automation"]["capabilities"][level]["model"]
                for level in CAPABILITY_LEVELS
            ],
            ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-sol"],
        )
        self.assertEqual(
            {
                config["execution_profiles"]["self_hosted_agent"]["providers"]["deepseek"]
                ["capabilities"][level]["model"]
                for level in CAPABILITY_LEVELS
            },
            {"deepseek-v4-flash", "deepseek-v4-pro"},
        )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "test.sqlite3"
        database = connect(self.db_path)
        initialize_schema(database)
        database.close()
        self.project_paths = [f"project-{index}" for index in range(1, 13)]

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

    def test_fresh_database_and_configuration_validate(self) -> None:
        database = connect(self.db_path)
        result = validate_database(database)
        database.close()
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["schema_version"], "3.5.0")

    def test_fresh_schema_has_capability_routing_and_execution_snapshot(self) -> None:
        database = connect(self.db_path)
        columns = {row[1] for row in database.execute("PRAGMA table_info(tasks)").fetchall()}
        execution_columns = {row[1] for row in database.execute("PRAGMA table_info(executions)").fetchall()}
        version = database.execute("PRAGMA user_version").fetchone()[0]
        database.close()
        self.assertIn("archived_at", columns)
        self.assertNotIn("execution_profile", columns)
        self.assertTrue({"capability_level", "provider_id", "execution_policy"}.issubset(columns))
        self.assertIn("runtime_environment", columns)
        self.assertTrue(
            {
                "runtime_environment", "provider_id", "capability_level", "execution_policy",
                "model", "reasoning", "attempt_timeout_seconds", "max_retries",
            }.issubset(execution_columns)
        )
        self.assertEqual(version, SCHEMA_USER_VERSION)

    def test_global_eight_parallel_claims_and_ninth_slot_full(self) -> None:
        targets = [
            ("routine", "codex_automation"), ("standard", "codex_automation"),
            ("advanced", "codex_automation"), ("deep", "codex_automation"),
            ("routine", "codex_cli"), ("standard", "codex_cli"),
            ("advanced", "deepseek"), ("complex", "deepseek"),
            ("complex", "codex_cli"),
        ]
        for index, (profile, environment) in enumerate(targets, start=1):
            self.add_task(
                f"TASK-{index}", f"project-{index}", execution_profile=profile,
                runtime_environment=environment,
            )
        with ThreadPoolExecutor(max_workers=9) as pool:
            results = list(
                pool.map(
                    lambda item: self.claim(f"exec-{item[0]}", item[1][0], item[1][1]),
                    enumerate(targets, start=1),
                )
            )
        outcomes = [result["outcome"] for result in results]
        self.assertEqual(outcomes.count("CLAIMED"), 8)
        self.assertEqual(outcomes.count("SLOT_FULL"), 1)
        full = next(result for result in results if result["outcome"] == "SLOT_FULL")
        self.assertEqual(full["limit_scope"], "global")
        self.assertEqual(full["maximum"], 8)

    def test_platform_parallel_limit_is_enforced_without_capability_pool(self) -> None:
        profiles = ["routine", "standard", "advanced", "deep", "complex", "routine"]
        for index, profile in enumerate(profiles, start=1):
            self.add_task(f"ROUTINE-{index}", f"project-{index}", execution_profile=profile)
        for index, profile in enumerate(profiles[:5], start=1):
            self.assertEqual(self.claim(f"platform-{index}", profile)["outcome"], "CLAIMED")
        full = self.claim("platform-6", profiles[5])
        self.assertEqual(full["outcome"], "SLOT_FULL")
        self.assertEqual(full["limit_scope"], "platform")
        self.assertEqual(full["platform_active"], 5)
        self.assertEqual(full["platform_maximum"], 5)

    def test_claim_isolated_by_execution_profile(self) -> None:
        self.add_task("STANDARD-BLOCKER", "project-1", "blocker", "standard")
        self.add_task("ROUTINE-LOW", "project-2", "low", "routine")
        result = self.claim("routine-only", "routine")
        self.assertEqual(result["task"]["id"], "ROUTINE-LOW")
        self.assertEqual(result["task"]["execution_profile"], "routine")

    def test_capability_claim_snapshots_resolved_configuration(self) -> None:
        self.add_task("CAPABILITY-SNAPSHOT", "project-1", execution_profile="advanced")
        result = self.run_ctl(
            "claim", "capability-snapshot", "--capability-level", "L3",
            "--runtime-environment", "codex_automation",
        )
        self.assertEqual(result["outcome"], "CLAIMED")
        database = connect(self.db_path)
        before = dict(
            database.execute(
                "SELECT runtime_environment, provider_id, capability_level, execution_policy, model, reasoning, "
                "attempt_timeout_seconds, max_retries FROM executions WHERE execution_id='capability-snapshot'"
            ).fetchone()
        )
        database.close()
        expected = resolve_execution_profile("codex_automation", None, "L3")
        self.assertEqual(before["model"], expected["model"])
        self.assertEqual(before["reasoning"], expected["reasoning"])
        changed_config = json.loads(json.dumps(load_initialization_config()))
        changed_config["execution_profiles"]["codex_automation"]["capabilities"]["L3"]["model"] = "future-model"
        self.assertEqual(
            resolve_execution_profile("codex_automation", None, "L3", changed_config)["model"],
            "future-model",
        )
        database = connect(self.db_path)
        after = dict(
            database.execute(
                "SELECT runtime_environment, provider_id, capability_level, execution_policy, model, reasoning, "
                "attempt_timeout_seconds, max_retries FROM executions WHERE execution_id='capability-snapshot'"
            ).fetchone()
        )
        database.close()
        self.assertEqual(after, before)

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

    def test_schema_33_accepts_legacy_claim_and_finish_parameters(self) -> None:
        legacy_path = Path(self.temporary.name) / "schema-33-claim.sqlite3"
        database = connect(legacy_path)
        database.executescript(self.schema_33())
        stamp = "2026-08-04T10:00:00.000+08:00"
        database.execute(
            "INSERT INTO tasks(id, title, description, status, priority, execution_profile, "
            "runtime_environment, created_at, updated_at) "
            "VALUES('LEGACY-CLAIM', 'legacy', '', 'PENDING', 'critical', 'complex', "
            "'codex_automation', ?, ?)",
            (stamp, stamp),
        )
        database.execute(
            "INSERT INTO task_scopes(task_id, ordinal, scope, scope_key) "
            "VALUES('LEGACY-CLAIM', 0, 'project-1/file.txt', 'project:project-1')"
        )
        database.close()
        claimed = self.run_ctl(
            "claim", "legacy-claim", "--profile", "complex",
            "--runtime-environment", "codex_automation", db_path=legacy_path,
        )
        self.assertEqual(claimed["outcome"], "CLAIMED")
        self.assertEqual(claimed["task"]["capability_level"], "L5")
        finished = self.run_ctl(
            "finish", "legacy-claim", "LEGACY-CLAIM", db_path=legacy_path,
            input_text=json.dumps({"status": "SUCCEEDED", "summary": "done", "verification": ["ok"]}),
        )
        self.assertEqual(finished["outcome"], "FINISHED")

    def test_capability_level_does_not_create_a_shared_capacity_pool(self) -> None:
        environments = ["codex_automation", "codex_cli", "deepseek"]
        for index, environment in enumerate(environments, start=1):
            self.add_task(f"ENV-{index}", f"project-{index}", runtime_environment=environment)
        self.add_task("ENV-4", "project-4", runtime_environment="codex_automation")
        for index, environment in enumerate(environments, start=1):
            self.assertEqual(self.claim(f"env-exec-{index}", runtime_environment=environment)["outcome"], "CLAIMED")
        fourth = self.claim("env-exec-4", runtime_environment="codex_automation")
        self.assertEqual(fourth["outcome"], "CLAIMED")

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

    def test_unmet_blocker_dependency_does_not_hide_ready_low_priority_task(self) -> None:
        self.add_task("DEPENDENCY", "project-1")
        self.add_task("BLOCKED-BLOCKER", "project-2", "blocker")
        self.add_task("READY-LOW", "project-3", "low")
        database = connect(self.db_path)
        database.execute("UPDATE tasks SET status='WAITING_HUMAN' WHERE id='DEPENDENCY'")
        database.execute(
            "INSERT INTO task_dependencies(task_id, dependency_id) VALUES('BLOCKED-BLOCKER', 'DEPENDENCY')"
        )
        database.close()
        claimed = self.claim("dependency-skip")
        self.assertEqual(claimed["task"]["id"], "READY-LOW")

    def test_duplicate_execution_id_is_rejected_atomically(self) -> None:
        self.add_task("DUPLICATE", "project-1")
        self.assertEqual(self.claim("same-execution")["outcome"], "CLAIMED")
        error = self.run_ctl_error(
            "claim", "same-execution", "--profile", "standard",
            "--runtime-environment", "codex_automation",
        )
        self.assertIn("execution-id 已存在", error["message"])

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

    def test_runner_confirmed_recovery_releases_capacity_and_scope(self) -> None:
        self.add_task("LEASE", "project-1", runtime_environment="codex_cli")
        self.claim("exec-old", runtime_environment="codex_cli")
        database = connect(self.db_path)
        database.execute("UPDATE executions SET lease_expires_at='2000-01-01T00:00:00+08:00' WHERE execution_id='exec-old'")
        database.execute("UPDATE scope_locks SET lease_expires_at='2000-01-01T00:00:00+08:00' WHERE execution_id='exec-old'")
        database.close()
        pending = self.claim("exec-blocked", runtime_environment="codex_cli")
        self.assertEqual(pending["outcome"], "NO_TASK")
        self.assertEqual(pending["recovery_required"][0]["recovery_confirmation"], "runner_confirmed_terminated")
        recovered = self.run_ctl("recover", "exec-old", "--runner-confirmed-terminated")
        self.assertEqual(recovered["outcome"], "RECOVERED")
        self.assertEqual(recovered["task_status"], "PENDING")
        result = self.claim("exec-new", runtime_environment="codex_cli")
        self.assertEqual(result["outcome"], "CLAIMED")
        self.assertEqual(result["task"]["id"], "LEASE")
        self.assertEqual(result["task"]["attempt"], 2)

    def test_heartbeat_renews_lease_without_creating_another_execution(self) -> None:
        self.add_task("HEARTBEAT", "project-1")
        claimed = self.claim("heartbeat-execution")
        database = connect(self.db_path)
        database.execute(
            "UPDATE executions SET lease_expires_at='2000-01-01T00:00:00+08:00' "
            "WHERE execution_id='heartbeat-execution'"
        )
        database.close()
        heartbeat = self.run_ctl("heartbeat", "heartbeat-execution", "HEARTBEAT")
        self.assertGreater(heartbeat["lease_expires_at"], claimed["lease_expires_at"])
        database = connect(self.db_path)
        self.assertEqual(
            database.execute("SELECT count(*) FROM executions WHERE status='RUNNING'").fetchone()[0], 1
        )
        self.assertEqual(
            database.execute("SELECT attempt FROM tasks WHERE id='HEARTBEAT'").fetchone()[0], 1
        )
        database.close()

    def test_attempt_timeout_is_reported_separately_from_heartbeat_and_lease(self) -> None:
        self.add_task("TIMED-OUT", "project-1")
        self.claim("timed-out-execution")
        self.add_task("OTHER", "project-2", priority="low")
        database = connect(self.db_path)
        database.execute(
            "UPDATE executions SET started_at='2000-01-01T00:00:00+08:00', heartbeat_at=?, "
            "lease_expires_at='2999-01-01T00:00:00+08:00' WHERE execution_id='timed-out-execution'",
            (now_shanghai(),),
        )
        database.close()
        result = self.claim("other-execution")
        timeout = next(
            item for item in result["recovery_required"]
            if item["execution_id"] == "timed-out-execution"
        )
        self.assertTrue(timeout["attempt_timed_out"])
        self.assertFalse(timeout["heartbeat_stalled"])
        self.assertFalse(timeout["lease_expired"])
        database = connect(self.db_path)
        execution = database.execute(
            "SELECT status, outcome, recovery_required FROM executions WHERE execution_id='timed-out-execution'"
        ).fetchone()
        task = database.execute("SELECT status FROM tasks WHERE id='TIMED-OUT'").fetchone()
        lock = database.execute(
            "SELECT status FROM scope_locks WHERE execution_id='timed-out-execution'"
        ).fetchone()
        database.close()
        self.assertEqual(tuple(execution), ("TIMED_OUT", "INFRASTRUCTURE_TIMEOUT", 1))
        self.assertEqual(task["status"], "WAITING_HUMAN")
        self.assertEqual(lock["status"], "QUARANTINED")

    def test_stalled_then_attempt_timeout_advances_without_reoccupying_capacity(self) -> None:
        self.add_task("STALE-THEN-TIMEOUT", "project-1")
        self.claim("exec-stale-timeout")
        self.add_task("OTHER-SCOPE", "project-2", priority="low")
        database = connect(self.db_path)
        database.execute(
            "UPDATE executions SET heartbeat_at='2000-01-01T00:00:00+08:00', "
            "lease_expires_at='2999-01-01T00:00:00+08:00' WHERE execution_id='exec-stale-timeout'"
        )
        database.close()

        other = self.claim("exec-other-scope")
        self.assertEqual(other["outcome"], "CLAIMED")
        database = connect(self.db_path)
        self.assertEqual(
            database.execute(
                "SELECT status FROM executions WHERE execution_id='exec-stale-timeout'"
            ).fetchone()[0],
            "STALLED",
        )
        self.assertEqual(
            database.execute("SELECT count(*) FROM executions WHERE status='RUNNING'").fetchone()[0],
            1,
        )
        database.execute(
            "UPDATE executions SET started_at='2000-01-01T00:00:00+08:00' "
            "WHERE execution_id='exec-stale-timeout'"
        )
        database.close()

        result = self.claim("exec-detect-timeout")
        self.assertEqual(result["outcome"], "RECOVERY_REQUIRED")
        recovery = result["recovery_required"][0]
        self.assertEqual(recovery["execution_status"], "TIMED_OUT")
        database = connect(self.db_path)
        self.assertEqual(
            database.execute(
                "SELECT status FROM executions WHERE execution_id='exec-stale-timeout'"
            ).fetchone()[0],
            "TIMED_OUT",
        )
        self.assertEqual(
            database.execute(
                "SELECT status FROM scope_locks WHERE execution_id='exec-stale-timeout'"
            ).fetchone()[0],
            "QUARANTINED",
        )
        self.assertEqual(
            database.execute("SELECT count(*) FROM executions WHERE status='RUNNING'").fetchone()[0],
            1,
        )
        database.close()

    def test_lease_expiry_is_independent_from_healthy_heartbeat(self) -> None:
        self.add_task("LEASE-FIRST", "project-1")
        self.claim("exec-lease-first")
        database = connect(self.db_path)
        database.execute(
            "UPDATE executions SET heartbeat_at=?, lease_expires_at='2000-01-01T00:00:00+08:00' "
            "WHERE execution_id='exec-lease-first'",
            (now_shanghai(),),
        )
        database.close()

        result = self.claim("exec-lease-detector")

        self.assertEqual(result["outcome"], "RECOVERY_REQUIRED")
        recovery = result["recovery_required"][0]
        self.assertTrue(recovery["lease_expired"])
        self.assertFalse(recovery["heartbeat_stalled"])
        self.assertFalse(recovery["attempt_timed_out"])

    def test_codex_stall_requires_human_safe_recovery_and_never_duplicates_scope(self) -> None:
        self.add_task("STALLED", "project-1")
        self.claim("exec-stalled")
        self.add_task("SAME-SCOPE", "project-1", priority="low")
        database = connect(self.db_path)
        database.execute(
            "UPDATE executions SET heartbeat_at='2000-01-01T00:00:00+08:00', "
            "lease_expires_at='2999-01-01T00:00:00+08:00' WHERE execution_id='exec-stalled'"
        )
        database.execute(
            "UPDATE tasks SET heartbeat_at='2000-01-01T00:00:00+08:00' WHERE id='STALLED'"
        )
        database.close()

        result = self.claim("exec-no-duplicate")
        self.assertEqual(result["outcome"], "CONFLICT")
        self.assertEqual(result["recovery_required"][0]["recovery_confirmation"], "human_confirmed_safe")
        database = connect(self.db_path)
        self.assertEqual(
            database.execute("SELECT status FROM executions WHERE execution_id='exec-stalled'").fetchone()[0],
            "STALLED",
        )
        self.assertEqual(
            database.execute("SELECT count(*) FROM scope_locks WHERE execution_id='exec-stalled'").fetchone()[0],
            1,
        )
        human_state = database.execute(
            "SELECT status, human_required, human_question FROM tasks WHERE id='STALLED'"
        ).fetchone()
        self.assertEqual(human_state["status"], "WAITING_HUMAN")
        self.assertEqual(human_state["human_required"], 1)
        self.assertIn("确认旧 Codex 客户端会话", human_state["human_question"])
        database.close()
        error = self.run_ctl_error("recover", "exec-stalled", "--runner-confirmed-terminated")
        self.assertIn("人工确认", error["message"])
        recovered = self.run_ctl("recover", "exec-stalled", "--human-confirmed-safe")
        self.assertEqual(recovered["task_status"], "PENDING")
        database = connect(self.db_path)
        self.assertEqual(
            database.execute("SELECT human_required FROM tasks WHERE id='STALLED'").fetchone()[0], 0
        )
        database.close()

    def test_stalled_blocker_does_not_release_conflicts_before_safe_recovery(self) -> None:
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
        self.assertEqual(result["task"]["id"], "NEW-RUNNABLE")
        self.assertEqual(result["recovery_required"][0]["execution_id"], "exec-blocker")
        database = connect(self.db_path)
        statuses = dict(
            database.execute(
                "SELECT id, status FROM tasks WHERE id IN ('CONFLICT-1', 'CONFLICT-2', 'NEW-RUNNABLE')"
            ).fetchall()
        )
        database.close()
        self.assertEqual(statuses["NEW-RUNNABLE"], "RUNNING")
        self.assertEqual(statuses["CONFLICT-1"], "WAITING_CONFLICT")
        self.assertEqual(statuses["CONFLICT-2"], "WAITING_CONFLICT")
        recovered = self.run_ctl("recover", "exec-blocker", "--human-confirmed-safe")
        self.assertEqual(set(recovered["requeued_conflicts"]), {"CONFLICT-1", "CONFLICT-2"})

    def test_recovery_failed_and_wait_actions_are_idempotent(self) -> None:
        self.add_task("RECOVERY-ACTIONS", "project-1")
        self.claim("exec-recovery-actions")
        database = connect(self.db_path)
        database.execute(
            "UPDATE executions SET heartbeat_at='2000-01-01T00:00:00+08:00' "
            "WHERE execution_id='exec-recovery-actions'"
        )
        database.close()
        self.claim("exec-recovery-detector")

        waiting = self.run_ctl(
            "recover", "exec-recovery-actions", "--human-confirmed-safe", "--action", "wait"
        )
        self.assertEqual(waiting["outcome"], "WAITING")
        repeated_wait = self.run_ctl(
            "recover", "exec-recovery-actions", "--human-confirmed-safe", "--action", "wait"
        )
        self.assertEqual(repeated_wait["outcome"], "ALREADY_WAITING")
        failed = self.run_ctl(
            "recover", "exec-recovery-actions", "--human-confirmed-safe", "--action", "failed"
        )
        self.assertEqual(failed["task_status"], "FAILED")
        repeated_failed = self.run_ctl(
            "recover", "exec-recovery-actions", "--human-confirmed-safe", "--action", "failed"
        )
        self.assertEqual(repeated_failed["outcome"], "ALREADY_RECOVERED")
        database = connect(self.db_path)
        history_count = database.execute(
            "SELECT count(*) FROM task_history WHERE task_id='RECOVERY-ACTIONS' "
            "AND actor='human-safe-recovery'"
        ).fetchone()[0]
        database.close()
        self.assertEqual(history_count, 2)

    def test_late_heartbeat_and_finish_are_fenced_after_quarantine_and_requeue(self) -> None:
        self.add_task("FENCED", "project-1")
        self.claim("exec-fenced-old")
        database = connect(self.db_path)
        database.execute(
            "UPDATE executions SET heartbeat_at='2000-01-01T00:00:00+08:00' "
            "WHERE execution_id='exec-fenced-old'"
        )
        database.close()
        self.claim("exec-fence-detector")

        heartbeat_error = self.run_ctl_error("heartbeat", "exec-fenced-old", "FENCED")
        report_path = Path(self.temporary.name) / "late-finish.json"
        report_path.write_text(
            json.dumps({"status": "SUCCEEDED", "summary": "late", "verification": ["late"]}),
            encoding="utf-8",
        )
        finish_error = self.run_ctl_error(
            "finish", "exec-fenced-old", "FENCED", str(report_path)
        )
        self.assertIn("活动 execution", heartbeat_error["message"])
        self.assertIn("活动 execution", finish_error["message"])

        self.run_ctl(
            "recover", "exec-fenced-old", "--human-confirmed-safe", "--action", "requeue"
        )
        claimed = self.claim("exec-fenced-new")
        self.assertEqual(claimed["task"]["id"], "FENCED")
        late_heartbeat = self.run_ctl_error("heartbeat", "exec-fenced-old", "FENCED")
        self.assertIn("活动 execution", late_heartbeat["message"])
        database = connect(self.db_path)
        lock = database.execute(
            "SELECT execution_id, status FROM scope_locks WHERE task_id='FENCED'"
        ).fetchone()
        database.close()
        self.assertEqual(tuple(lock), ("exec-fenced-new", "ACTIVE"))

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

    def test_human_answer_can_resolve_last_blocker_without_another_attempt(self) -> None:
        self.add_task("HUMAN-RESOLVE", "project-1")
        self.claim("exec-human-resolve")
        waiting_report = {
            "status": "WAITING_HUMAN",
            "summary": "构建产物已验证，只等待确认生产域名。",
            "verification": ["dist exists", "entry assets resolve"],
            "question": "生产域名是否正确？",
        }
        self.run_ctl(
            "finish",
            "exec-human-resolve",
            "HUMAN-RESOLVE",
            input_text=json.dumps(waiting_report, ensure_ascii=False),
        )

        result = self.run_ctl(
            "resolve-human",
            "HUMAN-RESOLVE",
            "--response",
            "该生产域名正确。",
        )
        self.assertEqual(result["outcome"], "HUMAN_RESOLVED")
        self.assertEqual(result["status"], "SUCCEEDED")

        database = connect(self.db_path)
        task = database.execute(
            "SELECT status, completed_at, progress_percent, result_summary, human_required, "
            "human_question, human_responded_at, human_response, attempt FROM tasks "
            "WHERE id='HUMAN-RESOLVE'"
        ).fetchone()
        history = database.execute(
            "SELECT from_status, to_status, actor FROM task_history "
            "WHERE task_id='HUMAN-RESOLVE' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        verification_count = database.execute(
            "SELECT count(*) FROM task_verifications WHERE task_id='HUMAN-RESOLVE'"
        ).fetchone()[0]
        database.close()
        self.assertEqual(task["status"], "SUCCEEDED")
        self.assertIsNotNone(task["completed_at"])
        self.assertEqual(task["progress_percent"], 100)
        self.assertEqual(task["result_summary"], waiting_report["summary"])
        self.assertEqual(task["human_required"], 0)
        self.assertEqual(task["human_question"], waiting_report["question"])
        self.assertIsNotNone(task["human_responded_at"])
        self.assertEqual(task["human_response"], "该生产域名正确。")
        self.assertEqual(task["attempt"], 1)
        self.assertEqual(tuple(history), ("WAITING_HUMAN", "SUCCEEDED", "human-resolution"))
        self.assertEqual(verification_count, 2)

    def test_human_resolution_rejects_unverified_or_unrelated_pending_task(self) -> None:
        self.add_task("UNVERIFIED-WAIT", "project-1")
        self.claim("exec-unverified-wait")
        self.run_ctl(
            "finish",
            "exec-unverified-wait",
            "UNVERIFIED-WAIT",
            input_text=json.dumps(
                {"status": "WAITING_HUMAN", "summary": "need input", "question": "continue?"}
            ),
        )
        error = self.run_ctl_error(
            "resolve-human", "UNVERIFIED-WAIT", "--response", "yes"
        )
        self.assertIn("缺少 Worker 验证记录", error["message"])

        self.add_task("PLAIN-PENDING", "project-2")
        error = self.run_ctl_error(
            "resolve-human",
            "PLAIN-PENDING",
            "--response",
            "done",
            "--summary",
            "done",
        )
        self.assertIn("刚从 WAITING_HUMAN 误重排", error["message"])

    def test_just_requeued_human_task_can_be_resolved_before_new_claim(self) -> None:
        self.add_task("REQUEUED-RESOLVE", "project-1")
        self.claim("exec-requeued-resolve")
        self.run_ctl(
            "finish",
            "exec-requeued-resolve",
            "REQUEUED-RESOLVE",
            input_text=json.dumps(
                {
                    "status": "WAITING_HUMAN",
                    "summary": "build complete",
                    "verification": ["artifact verified"],
                    "question": "is endpoint approved?",
                }
            ),
        )
        self.run_ctl("requeue", "REQUEUED-RESOLVE", "--reason", "answer supplied")
        result = self.run_ctl(
            "resolve-human",
            "REQUEUED-RESOLVE",
            "--response",
            "endpoint approved",
            "--summary",
            "Production build completed and configuration was approved.",
        )
        self.assertEqual(result["status"], "SUCCEEDED")
        database = connect(self.db_path)
        task = database.execute(
            "SELECT status, attempt, result_summary FROM tasks WHERE id='REQUEUED-RESOLVE'"
        ).fetchone()
        database.close()
        self.assertEqual(tuple(task), (
            "SUCCEEDED", 1, "Production build completed and configuration was approved."
        ))

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

    def test_migrate_schema_34_preserves_active_execution_without_guessing_quarantine(self) -> None:
        legacy_path = Path(self.temporary.name) / "schema-34-active.sqlite3"
        database = connect(legacy_path)
        database.executescript(self.schema_34())
        stamp = now_shanghai()
        database.execute(
            """INSERT INTO tasks(
              id, title, status, priority, capability_level, runtime_environment, provider_id,
              execution_policy, assigned_agent, created_at, started_at, updated_at, heartbeat_at, attempt
            ) VALUES('ACTIVE-34', 'active', 'RUNNING', 'blocker', 'L5', 'codex_automation', NULL,
              'automatic', 'active-34-execution', ?, ?, ?, ?, 1)""",
            (stamp, stamp, stamp, stamp),
        )
        database.execute(
            "INSERT INTO task_scopes(task_id, ordinal, scope, scope_key) "
            "VALUES('ACTIVE-34', 0, 'local-agent-loop/scripts/loopctl.py', 'project:local-agent-loop')"
        )
        database.execute(
            """INSERT INTO executions(
              execution_id, task_id, status, started_at, heartbeat_at, lease_expires_at,
              runtime_environment, provider_id, capability_level, execution_policy, model, reasoning,
              attempt_timeout_seconds, max_retries
            ) VALUES('active-34-execution', 'ACTIVE-34', 'RUNNING', ?, ?, '2999-01-01T00:00:00+08:00',
              'codex_automation', NULL, 'L5', 'automatic', 'gpt-5.6-sol', 'xhigh', 14400, 0)""",
            (stamp, stamp),
        )
        database.execute(
            """INSERT INTO scope_locks(
              scope_key, task_id, execution_id, acquired_at, lease_expires_at
            ) VALUES('project:local-agent-loop', 'ACTIVE-34', 'active-34-execution', ?,
              '2999-01-01T00:00:00+08:00')""",
            (stamp,),
        )
        database.close()

        migrated = self.run_ctl("migrate", db_path=legacy_path)

        self.assertEqual(migrated["from"], "3.4.0")
        self.assertEqual(migrated["to"], "3.5.0")
        self.assertEqual(migrated["active_executions_preserved"], 1)
        self.assertEqual(migrated["quarantines_created"], 0)
        database = connect(legacy_path)
        execution = database.execute(
            "SELECT status, recovery_required FROM executions WHERE execution_id='active-34-execution'"
        ).fetchone()
        lock = database.execute(
            "SELECT status, quarantined_at FROM scope_locks WHERE execution_id='active-34-execution'"
        ).fetchone()
        validation = validate_database(database)
        database.close()
        self.assertEqual(tuple(execution), ("RUNNING", 0))
        self.assertEqual(tuple(lock), ("ACTIVE", None))
        self.assertTrue(validation["ok"], validation["errors"])

    def test_migrate_schema_33_maps_routing_and_snapshots_execution(self) -> None:
        legacy_path = Path(self.temporary.name) / "schema-33.sqlite3"
        database = connect(legacy_path)
        database.executescript(self.schema_33())
        stamp = "2026-08-03T09:00:00.000+08:00"
        database.executemany(
            "INSERT INTO tasks(id, title, description, status, priority, execution_profile, "
            "runtime_environment, created_at, updated_at) VALUES(?, ?, '', 'FAILED', 'high', ?, ?, ?, ?)",
            [
                ("OLD-ADVANCED", "advanced", "advanced", "codex_cli", stamp, stamp),
                ("OLD-EXCEPTIONAL", "exceptional", "exceptional", "deepseek", stamp, stamp),
            ],
        )
        database.execute(
            "INSERT INTO executions(execution_id, task_id, status, started_at, heartbeat_at, lease_expires_at, "
            "finished_at, outcome) VALUES('old-execution', 'OLD-EXCEPTIONAL', 'FINISHED', ?, ?, ?, ?, 'FAILED')",
            (stamp, stamp, stamp, stamp),
        )
        database.close()

        migrated = self.run_ctl("migrate", db_path=legacy_path)
        self.assertEqual(migrated["outcome"], "MIGRATED")
        self.assertEqual(migrated["from"], "3.3.0")
        self.assertEqual(migrated["to"], "3.5.0")
        self.assertEqual(migrated["tasks_mapped"], 2)
        self.assertEqual(migrated["executions_snapshotted"], 1)
        database = connect(legacy_path)
        rows = {
            row["id"]: dict(row)
            for row in database.execute(
                "SELECT id, capability_level, runtime_environment, provider_id, execution_policy FROM tasks"
            ).fetchall()
        }
        snapshot = dict(database.execute("SELECT * FROM executions WHERE execution_id='old-execution'").fetchone())
        columns = {row[1] for row in database.execute("PRAGMA table_info(tasks)").fetchall()}
        database.close()
        self.assertEqual(
            rows["OLD-ADVANCED"],
            {"id": "OLD-ADVANCED", "capability_level": "L3", "runtime_environment": "codex_cli",
             "provider_id": None, "execution_policy": "automatic"},
        )
        self.assertEqual(
            rows["OLD-EXCEPTIONAL"],
            {"id": "OLD-EXCEPTIONAL", "capability_level": "L5",
             "runtime_environment": "self_hosted_agent", "provider_id": "deepseek",
             "execution_policy": "manual"},
        )
        self.assertNotIn("execution_profile", columns)
        self.assertEqual(snapshot["capability_level"], "L5")
        self.assertEqual(snapshot["provider_id"], "deepseek")
        self.assertEqual(snapshot["model"], "deepseek-v4-pro")
        self.assertEqual(snapshot["max_retries"], 2)
        self.assertEqual(self.run_ctl("migrate", db_path=legacy_path)["outcome"], "ALREADY_CURRENT")

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
        self.assertEqual(task["runtime_environment"], "self_hosted_agent")
        self.assertEqual(task["provider_id"], "deepseek")

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

    def test_provider_is_required_only_for_self_hosted_agent(self) -> None:
        task_path = Path(self.temporary.name) / "provider-task.json"
        base = {
            "id": "PROVIDER-RULE", "title": "provider", "capability_level": "L2",
            "scope": ["local-agent-loop/scripts/loopctl.py"], "acceptance": ["test"],
        }
        task_path.write_text(
            json.dumps({**base, "runtime_environment": "self_hosted_agent"}), encoding="utf-8"
        )
        self.assertIn("必须提供 provider_id", self.run_ctl_error("enqueue", str(task_path))["message"])
        task_path.write_text(
            json.dumps({**base, "runtime_environment": "codex_cli", "provider_id": "deepseek"}),
            encoding="utf-8",
        )
        self.assertIn("不得保存 provider_id", self.run_ctl_error("enqueue", str(task_path))["message"])
        task_path.write_text(
            json.dumps(
                {**base, "runtime_environment": "self_hosted_agent", "provider_id": "deepseek"}
            ),
            encoding="utf-8",
        )
        self.assertEqual(self.run_ctl("enqueue", str(task_path))["outcome"], "ENQUEUED")

    def test_invalid_execution_profile_config_is_rejected(self) -> None:
        config = load_initialization_config()
        config["execution_profiles"]["codex_cli"]["capabilities"]["L2"].pop("max_retries")
        config_path = Path(self.temporary.name) / "invalid-config.json"
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(LoopError):
            load_initialization_config(config_path)

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
            "task_verifications", "task_attachments", "task_history",
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
        self.assertEqual(migrated["to"], "3.5.0")
        self.assertEqual(migrated["tasks_mapped"], 2)
        self.assertEqual(migrated["executions_snapshotted"], 1)

        database = connect(schema_32_path)
        tasks_after = {
            row["id"]: dict(row) for row in database.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        }
        for task_id, before in tasks_before.items():
            after = tasks_after[task_id]
            legacy_profile = before.pop("execution_profile")
            expected_level = {"advanced": "L3", "deep": "L4"}[legacy_profile]
            self.assertEqual(after.pop("capability_level"), expected_level)
            self.assertEqual(after.pop("runtime_environment"), "codex_automation")
            self.assertIsNone(after.pop("provider_id"))
            self.assertEqual(after.pop("execution_policy"), "automatic")
            self.assertEqual(after, before)
        children_after = {
            table: [tuple(row) for row in database.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]
            for table in child_tables
        }
        execution = dict(database.execute("SELECT * FROM executions WHERE execution_id='old-execution'").fetchone())
        database.close()
        self.assertEqual(children_after, children_before)
        self.assertEqual(
            tuple(execution[key] for key in (
                "execution_id", "task_id", "status", "started_at", "heartbeat_at",
                "lease_expires_at", "finished_at", "outcome",
            )),
            ("old-execution", "SCHEMA-32-ROUTED", "FINISHED", stamp, stamp, stamp, stamp, "FAILED"),
        )
        self.assertEqual(execution["capability_level"], "L4")

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
        database = connect(schema_32_path)
        self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 30200)
        self.assertIn(
            "execution_profile",
            {row[1] for row in database.execute("PRAGMA table_info(tasks)").fetchall()},
        )
        self.assertEqual(database.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
        database.close()

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
    def schema_34() -> str:
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        schema = schema.replace("PRAGMA user_version = 30500;", "PRAGMA user_version = 30400;")
        schema = schema.replace(
            "  status TEXT NOT NULL CHECK (status IN (\n"
            "    'RUNNING', 'FINISHED', 'EXPIRED', 'STALLED', 'TIMED_OUT'\n"
            "  )),\n",
            "  status TEXT NOT NULL CHECK (status IN ('RUNNING', 'FINISHED', 'EXPIRED')),\n",
        )
        schema = schema.replace(
            "  termination_reason TEXT,\n"
            "  recovery_required INTEGER NOT NULL DEFAULT 0 CHECK (recovery_required IN (0, 1)),\n"
            "  recovered_at TEXT,\n"
            "  recovery_action TEXT CHECK (recovery_action IS NULL OR recovery_action IN (\n"
            "    'requeue', 'failed', 'wait'\n"
            "  )),\n",
            "",
        )
        schema = schema.replace(
            "  lease_expires_at TEXT NOT NULL,\n"
            "  status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'QUARANTINED')),\n"
            "  quarantined_at TEXT,\n"
            "  quarantine_reason TEXT,\n"
            "  CHECK (\n"
            "    (status = 'ACTIVE' AND quarantined_at IS NULL AND quarantine_reason IS NULL)\n"
            "    OR (status = 'QUARANTINED' AND quarantined_at IS NOT NULL AND length(trim(quarantine_reason)) > 0)\n"
            "  )\n",
            "  lease_expires_at TEXT NOT NULL\n",
        )
        return schema

    @staticmethod
    def schema_33() -> str:
        schema = LoopConcurrencyTests.schema_34()
        schema = schema.replace("PRAGMA user_version = 30400;", "PRAGMA user_version = 30300;")
        schema = schema.replace(
            "  capability_level TEXT NOT NULL DEFAULT 'L2' CHECK (capability_level IN (\n"
            "    'L1', 'L2', 'L3', 'L4', 'L5'\n"
            "  )),\n"
            "  runtime_environment TEXT NOT NULL CHECK (runtime_environment IN (\n"
            "    'codex_automation', 'codex_cli', 'self_hosted_agent'\n"
            "  )),\n"
            "  provider_id TEXT,\n"
            "  execution_policy TEXT NOT NULL DEFAULT 'automatic' CHECK (execution_policy IN (\n"
            "    'automatic', 'manual'\n"
            "  )),\n",
            "  execution_profile TEXT NOT NULL DEFAULT 'standard' CHECK (execution_profile IN (\n"
            "    'routine', 'standard', 'advanced', 'deep', 'complex', 'exceptional'\n"
            "  )),\n"
            "  runtime_environment TEXT NOT NULL CHECK (runtime_environment IN (\n"
            "    'codex_automation', 'codex_cli', 'deepseek'\n"
            "  )),\n",
        )
        schema = schema.replace(
            "  row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version >= 1),\n"
            "  CHECK (\n"
            "    (runtime_environment = 'self_hosted_agent' AND provider_id IS NOT NULL AND length(trim(provider_id)) > 0)\n"
            "    OR (runtime_environment <> 'self_hosted_agent' AND provider_id IS NULL)\n"
            "  )\n",
            "  row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version >= 1)\n",
        )
        schema = schema.replace(
            "  outcome TEXT,\n"
            "  runtime_environment TEXT NOT NULL CHECK (runtime_environment IN (\n"
            "    'codex_automation', 'codex_cli', 'self_hosted_agent'\n"
            "  )),\n"
            "  provider_id TEXT,\n"
            "  capability_level TEXT NOT NULL CHECK (capability_level IN ('L1', 'L2', 'L3', 'L4', 'L5')),\n"
            "  execution_policy TEXT NOT NULL CHECK (execution_policy IN ('automatic', 'manual')),\n"
            "  model TEXT NOT NULL,\n"
            "  reasoning TEXT NOT NULL CHECK (reasoning IN ('low', 'medium', 'high', 'xhigh')),\n"
            "  attempt_timeout_seconds INTEGER NOT NULL CHECK (attempt_timeout_seconds > 0),\n"
            "  max_retries INTEGER NOT NULL CHECK (max_retries >= 0),\n"
            "  CHECK (\n"
            "    (runtime_environment = 'self_hosted_agent' AND provider_id IS NOT NULL AND length(trim(provider_id)) > 0)\n"
            "    OR (runtime_environment <> 'self_hosted_agent' AND provider_id IS NULL)\n"
            "  )\n",
            "  outcome TEXT\n",
        )
        schema = schema.replace(
            "  lease_expires_at TEXT NOT NULL,\n"
            "  status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'QUARANTINED')),\n"
            "  quarantined_at TEXT,\n"
            "  quarantine_reason TEXT,\n"
            "  CHECK (\n"
            "    (status = 'ACTIVE' AND quarantined_at IS NULL AND quarantine_reason IS NULL)\n"
            "    OR (status = 'QUARANTINED' AND quarantined_at IS NOT NULL AND length(trim(quarantine_reason)) > 0)\n"
            "  )\n",
            "  lease_expires_at TEXT NOT NULL\n",
        )
        schema = schema.replace(
            "  ON tasks(status, runtime_environment, provider_id, capability_level, execution_policy, priority, created_at, id);",
            "  ON tasks(status, runtime_environment, execution_profile, priority, created_at, id);",
        )
        return schema

    @staticmethod
    def schema_32() -> str:
        schema = LoopConcurrencyTests.schema_33()
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
