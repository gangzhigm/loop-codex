from __future__ import annotations

# 中文排查：旧 JSON、SQLite Schema 与混合锁迁移回归。
# 公共 fixture 位于 _loop_support.py；业务行为断言保留在各职责模块中。

from _loop_support import *  # noqa: F403


class LoopMigrationTests(LoopTestCase):
    def test_legacy_json_import_uses_explicit_migration_command(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        completed = subprocess.run(
            [sys.executable, str(LOOPCTL), "--help"],
            cwd=BASE_DIR,
            text=True,
            encoding="utf-8",
            errors="strict",
            capture_output=True,
            check=False,
            timeout=30,
            env=environment,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("migrate-legacy", completed.stdout)
        self.assertNotIn("{init,", completed.stdout)

    def test_migrate_schema_36_moves_old_draft_to_review_and_keeps_pending_ready(self) -> None:
        legacy_path = Path(self.temporary.name) / "schema-36.sqlite3"
        database = connect(legacy_path)
        database.executescript(self.schema_36())
        for task_id, status in (("OLD-DRAFT", "DRAFT"), ("OLD-PENDING", "PENDING")):
            insert_task(
                database,
                {
                    "id": task_id,
                    "title": task_id,
                    "description": "legacy business fact",
                    "status": status,
                    "priority": "critical",
                    "capability_level": "L4",
                    "runtime_environment": "codex_automation",
                    "execution_policy": "automatic",
                    "created_at": now_shanghai(),
                    "scope": ["local-agent-loop/scripts/loopdb.py"],
                    "acceptance": ["legacy acceptance"],
                },
                project_paths=["local-agent-loop"],
            )
        database.close()

        migrated = self.run_ctl("migrate", db_path=legacy_path)
        self.assertEqual((migrated["from"], migrated["to"]), ("3.6.0", "3.7.0"))
        self.assertEqual(migrated["old_drafts_moved_to_review"], 1)
        database = connect(legacy_path)
        draft = database.execute(
            "SELECT status, preflight_status, estimated_capability_level, capability_level, lock_mode "
            "FROM tasks WHERE id='OLD-DRAFT'"
        ).fetchone()
        pending = database.execute(
            "SELECT status, preflight_status, estimated_capability_level, capability_level, lock_mode "
            "FROM tasks WHERE id='OLD-PENDING'"
        ).fetchone()
        history = database.execute(
            "SELECT from_status, to_status FROM task_history WHERE task_id='OLD-DRAFT' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        validation = validate_database(database)
        database.close()
        self.assertEqual(tuple(draft), ("NEEDS_REVIEW", "FAILED", "L4", None, None))
        self.assertEqual(tuple(pending), ("PENDING", "READY", "L4", "L4", "project"))
        self.assertEqual(tuple(history), ("DRAFT", "NEEDS_REVIEW"))
        self.assertTrue(validation["ok"], validation["errors"])

    def test_migrate_schema_35_adds_diagnostic_column_and_preserves_active_execution(self) -> None:
        legacy_path = Path(self.temporary.name) / "schema-35-active.sqlite3"
        database = connect(legacy_path)
        database.executescript(self.schema_35())
        stamp = now_shanghai()
        insert_task(
            database,
            {
                "id": "ACTIVE-35",
                "title": "active",
                "status": "RUNNING",
                "priority": "blocker",
                "capability_level": "L5",
                "runtime_environment": "codex_automation",
                "execution_policy": "automatic",
                "assigned_agent": "active-35-execution",
                "created_at": stamp,
                "started_at": stamp,
                "updated_at": stamp,
                "heartbeat_at": stamp,
                "attempt": 1,
                "scope": ["local-agent-loop/scripts/loopctl.py"],
            },
            project_paths=["local-agent-loop"],
        )
        database.execute(
            """INSERT INTO executions(
              execution_id, task_id, status, started_at, heartbeat_at, lease_expires_at,
              runtime_environment, provider_id, capability_level, execution_policy, model, reasoning,
              attempt_timeout_seconds, max_retries
            ) VALUES('active-35-execution', 'ACTIVE-35', 'RUNNING', ?, ?,
              '2999-01-01T00:00:00+08:00', 'codex_automation', NULL, 'L5', 'automatic',
              'gpt-5.6-sol', 'xhigh', 3600, 1)""",
            (stamp, stamp),
        )
        database.execute(
            """INSERT INTO scope_locks(
              scope_key, task_id, execution_id, acquired_at, lease_expires_at, status
            ) VALUES('project:local-agent-loop', 'ACTIVE-35', 'active-35-execution', ?,
              '2999-01-01T00:00:00+08:00', 'ACTIVE')""",
            (stamp,),
        )
        database.close()

        migrated = self.run_ctl("migrate", db_path=legacy_path)

        self.assertEqual(migrated["from"], "3.5.0")
        self.assertEqual(migrated["to"], "3.7.0")
        self.assertEqual(migrated["active_executions_preserved"], 1)
        database = connect(legacy_path)
        columns = {row[1] for row in database.execute("PRAGMA table_info(tasks)")}
        execution = database.execute(
            "SELECT status FROM executions WHERE execution_id='active-35-execution'"
        ).fetchone()[0]
        lock = database.execute(
            "SELECT status FROM scope_locks WHERE execution_id='active-35-execution'"
        ).fetchone()[0]
        validation = validate_database(database)
        database.close()
        self.assertIn("result_diagnostic_json", columns)
        self.assertEqual(execution, "RUNNING")
        self.assertEqual(lock, "ACTIVE")
        self.assertTrue(validation["ok"], validation["errors"])

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
        self.assertEqual(migrated["to"], "3.7.0")
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
        self.assertEqual(migrated["to"], "3.7.0")
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
        self.assertEqual(migrated["to"], "3.7.0")
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
            self.assertEqual(after.pop("estimated_capability_level"), expected_level)
            self.assertEqual(after.pop("runtime_environment"), "codex_automation")
            self.assertIsNone(after.pop("provider_id"))
            self.assertEqual(after.pop("execution_policy"), "automatic")
            self.assertIsNone(after.pop("result_diagnostic_json"))
            has_scope = task_id == "SCHEMA-32-ROUTED"
            self.assertEqual(after.pop("preflight_status"), "READY" if has_scope else "FAILED")
            self.assertIsNone(after.pop("preflight_execution_id"))
            self.assertIsNone(after.pop("preflight_started_at"))
            completed_at = after.pop("preflight_completed_at")
            failure = after.pop("preflight_failure")
            self.assertEqual(completed_at is not None, has_scope)
            self.assertEqual(failure is None, has_scope)
            self.assertEqual(
                json.loads(after.pop("scope_hint_json")),
                ["local-agent-loop/scripts/loopdb.py"] if has_scope else [],
            )
            self.assertEqual(after.pop("lock_mode"), "project" if has_scope else None)
            self.assertEqual(after.pop("split_suggestions_json"), "[]")
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

    def test_same_version_hybrid_lock_migration_requeues_legacy_conflict_and_preserves_audit(self) -> None:
        legacy_path = Path(self.temporary.name) / "schema-37-project-lock.sqlite3"
        legacy_schema = SCHEMA_PATH.read_text(encoding="utf-8").replace(
            "lock_mode TEXT CHECK (lock_mode IS NULL OR lock_mode IN ('file', 'module', 'project'))",
            "lock_mode TEXT CHECK (lock_mode IS NULL OR lock_mode IN ('project'))",
        )
        database = connect(legacy_path)
        database.executescript(legacy_schema)
        for task_id, priority in (("LEGACY-BLOCKER", "critical"), ("LEGACY-WAIT", "high")):
            insert_task(
                database,
                {
                    "id": task_id,
                    "title": task_id,
                    "status": "PENDING",
                    "priority": priority,
                    "runtime_environment": "codex_automation",
                    "scope": ["local-agent-loop/scripts/loopctl.py"],
                    "acceptance": ["test"],
                },
                project_paths=["local-agent-loop"],
            )
        insert_task(
            database,
            {
                "id": "LEGACY-EXTERNAL",
                "title": "legacy external scope",
                "status": "PENDING",
                "priority": "medium",
                "runtime_environment": "codex_automation",
                "scope": ["local-agent-loop/scripts/loopctl.py"],
                "acceptance": ["test"],
            },
            project_paths=["local-agent-loop"],
        )
        database.execute(
            "UPDATE task_scopes SET scope='OSS:Zaun_01/path/template.xlsx', "
            "scope_key='external:OSS:Zaun_01/path/template.xlsx' "
            "WHERE task_id='LEGACY-EXTERNAL'"
        )
        database.execute("UPDATE tasks SET status='WAITING_CONFLICT' WHERE id='LEGACY-WAIT'")
        database.execute(
            "INSERT INTO task_conflicts(task_id, scope_key, blocker_task_id, blocker_execution_id, detected_at) "
            "VALUES('LEGACY-WAIT', 'project:local-agent-loop', 'LEGACY-BLOCKER', 'legacy-exec', ?)",
            (now_shanghai(),),
        )
        database.close()

        migrated = self.run_ctl("migrate", db_path=legacy_path)

        self.assertTrue(migrated["hybrid_scope_lock_migrated"])
        self.assertEqual(migrated["waiting_conflicts_requeued"], ["LEGACY-WAIT"])
        database = connect(legacy_path)
        status = database.execute(
            "SELECT status FROM tasks WHERE id='LEGACY-WAIT'"
        ).fetchone()[0]
        audit_count = database.execute(
            "SELECT count(*) FROM task_conflicts WHERE task_id='LEGACY-WAIT'"
        ).fetchone()[0]
        history = database.execute(
            "SELECT from_status, to_status, actor FROM task_history "
            "WHERE task_id='LEGACY-WAIT' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        external_scope = database.execute(
            "SELECT scope_key FROM task_scopes WHERE task_id='LEGACY-EXTERNAL'"
        ).fetchone()[0]
        database.execute("UPDATE tasks SET lock_mode='file' WHERE id='LEGACY-WAIT'")
        database.execute(
            "UPDATE task_scopes SET scope_key='file:local-agent-loop::scripts/loopctl.py' "
            "WHERE task_id='LEGACY-WAIT'"
        )
        validation = validate_database(database)
        database.close()
        self.assertEqual(status, "PENDING")
        self.assertEqual(audit_count, 1)
        self.assertEqual(tuple(history), ("WAITING_CONFLICT", "PENDING", "schema-migration"))
        self.assertEqual(external_scope, "external:OSS:Zaun_01/path/template.xlsx")
        self.assertTrue(validation["ok"], validation["errors"])


if __name__ == "__main__":
    unittest.main()
