from __future__ import annotations

# 中文排查：领取、容量、优先级、依赖与 scope 锁回归。
# 公共 fixture 位于 _loop_support.py；业务行为断言保留在各职责模块中。

from _loop_support import *  # noqa: F403


class LoopClaimingTests(LoopTestCase):
    def test_global_eight_parallel_claims_and_ninth_slot_full(self) -> None:
        targets = [
            ("L1", "codex_cli", None), ("L2", "codex_cli", None),
            ("L3", "codex_cli", None), ("L4", "codex_cli", None),
            ("L5", "codex_cli", None),
            ("L1", "self_hosted_agent", "deepseek"),
            ("L2", "self_hosted_agent", "deepseek"),
            ("L3", "self_hosted_agent", "deepseek"),
            ("L4", "self_hosted_agent", "deepseek"),
        ]
        for index, (level, environment, provider_id) in enumerate(targets, start=1):
            self.add_task(
                f"TASK-{index}", f"project-{index}", capability_level=level,
                runtime_environment=environment, provider_id=provider_id,
            )
        with ThreadPoolExecutor(max_workers=9) as pool:
            results = list(
                pool.map(
                    lambda item: self.claim(
                        f"exec-{item[0]}", item[1][0], item[1][1], item[1][2]
                    ),
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
        levels = ["L1", "L2", "L3", "L4", "L5", "L1"]
        for index, level in enumerate(levels, start=1):
            self.add_task(f"TASK-{index}", f"project-{index}", capability_level=level)
        for index, level in enumerate(levels[:5], start=1):
            self.assertEqual(self.claim(f"platform-{index}", level)["outcome"], "CLAIMED")
        full = self.claim("platform-6", levels[5])
        self.assertEqual(full["outcome"], "SLOT_FULL")
        self.assertEqual(full["limit_scope"], "platform")
        self.assertEqual(full["platform_active"], 5)
        self.assertEqual(full["platform_maximum"], 5)

    def test_claim_isolated_by_capability_level(self) -> None:
        self.add_task("L2-BLOCKER", "project-1", "blocker", "L2")
        self.add_task("L1-LOW", "project-2", "low", "L1")
        result = self.claim("l1-only", "L1")
        self.assertEqual(result["task"]["id"], "L1-LOW")
        self.assertEqual(result["task"]["capability_level"], "L1")

    def test_capability_claim_snapshots_resolved_configuration(self) -> None:
        self.add_task("CAPABILITY-SNAPSHOT", "project-1", capability_level="L3")
        result = self.run_ctl(
            "claim", "capability-snapshot", "--capability-level", "L3",
            "--runtime-environment", "codex_cli",
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
        expected = resolve_execution_profile("codex_cli", None, "L3")
        self.assertEqual(before["model"], expected["model"])
        self.assertEqual(before["reasoning"], expected["reasoning"])
        changed_config = json.loads(json.dumps(load_initialization_config()))
        changed_config["execution_profiles"]["codex_cli"]["capabilities"]["L3"]["model"] = "future-model"
        self.assertEqual(
            resolve_execution_profile("codex_cli", None, "L3", changed_config)["model"],
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
        self.add_task(
            "SELF-HOSTED-BLOCKER", "project-1", "blocker",
            runtime_environment="self_hosted_agent", provider_id="deepseek",
        )
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
            [sys.executable, str(LOOPCTL), "--db", str(self.db_path), "claim", "missing-env", "--capability-level", "L2"],
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

    def test_capability_level_does_not_create_a_shared_capacity_pool(self) -> None:
        self.add_task("ENV-1", "project-1", runtime_environment="codex_cli")
        self.add_task(
            "ENV-2", "project-2", runtime_environment="self_hosted_agent", provider_id="deepseek"
        )
        self.add_task("ENV-3", "project-3", runtime_environment="codex_cli")
        self.assertEqual(self.claim("env-exec-1", runtime_environment="codex_cli")["outcome"], "CLAIMED")
        self.assertEqual(
            self.claim(
                "env-exec-2", runtime_environment="self_hosted_agent", provider_id="deepseek"
            )["outcome"],
            "CLAIMED",
        )
        self.assertEqual(self.claim("env-exec-3", runtime_environment="codex_cli")["outcome"], "CLAIMED")

    def test_scope_conflict_is_shared_across_runtime_environments(self) -> None:
        self.add_task("CLI-LOCK", "project-1", "critical", runtime_environment="codex_cli")
        self.add_task(
            "AGENT-CONFLICT", "project-1", "high",
            runtime_environment="self_hosted_agent", provider_id="deepseek",
        )
        self.assertEqual(self.claim("cli-lock")["outcome"], "CLAIMED")
        conflict = self.claim(
            "agent-conflict", runtime_environment="self_hosted_agent", provider_id="deepseek"
        )
        self.assertEqual(conflict["outcome"], "CONFLICT")
        self.assertEqual(conflict["task_id"], "AGENT-CONFLICT")

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
            "claim", "same-execution", "--capability-level", "L2",
            "--runtime-environment", "codex_cli",
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
        self.assertEqual(finished["requeued_conflicts"], [])
        database = connect(self.db_path)
        status = database.execute("SELECT status FROM tasks WHERE id='CONFLICT'").fetchone()[0]
        database.close()
        self.assertEqual(status, "PENDING")

    def test_claim_compatibly_converts_legacy_waiting_conflict_without_losing_audit(self) -> None:
        self.add_task("LEGACY-CONFLICT", "project-1", "high")
        self.add_task("LEGACY-BLOCKER", "project-2", "low")
        database = connect(self.db_path)
        database.execute("UPDATE tasks SET status='WAITING_CONFLICT' WHERE id='LEGACY-CONFLICT'")
        database.execute(
            "INSERT INTO task_conflicts(task_id, scope_key, blocker_task_id, blocker_execution_id, detected_at) "
            "VALUES('LEGACY-CONFLICT', 'project:project-1', 'LEGACY-BLOCKER', 'old-execution', ?)",
            (now_shanghai(),),
        )
        database.close()

        claimed = self.claim("exec-legacy-conflict")

        self.assertEqual(claimed["task"]["id"], "LEGACY-CONFLICT")
        self.assertEqual(claimed["requeued"], ["LEGACY-CONFLICT"])
        database = connect(self.db_path)
        audit_count = database.execute(
            "SELECT count(*) FROM task_conflicts WHERE task_id='LEGACY-CONFLICT'"
        ).fetchone()[0]
        transition = database.execute(
            "SELECT from_status, to_status, actor FROM task_history "
            "WHERE task_id='LEGACY-CONFLICT' AND actor='conflict-compatibility'"
        ).fetchone()
        database.close()
        self.assertEqual(audit_count, 1)
        self.assertEqual(tuple(transition), ("WAITING_CONFLICT", "PENDING", "conflict-compatibility"))

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
        self.assertEqual(conflict_status, "PENDING")

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
                "runtime_environment": "codex_cli",
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

    def test_windows_scope_normalization_and_hierarchical_conflicts(self) -> None:
        normalized = normalize_scope(
            "PROJECT-1\\src\\.\\Feature.py", "file", self.project_paths
        )
        self.assertEqual(normalized["scope"], "project-1/src/Feature.py")
        self.assertEqual(normalized["scope_key"], "file:project-1::src/feature.py")
        self.assertTrue(scope_keys_conflict(
            "module:project-1::src", "file:project-1::src/feature.py"
        ))
        self.assertTrue(scope_keys_conflict(
            "module:project-1::src", "module:project-1::src/components"
        ))
        self.assertFalse(scope_keys_conflict(
            "file:project-1::src/a.py", "file:project-1::src/b.py"
        ))
        for unsafe in (
            "project-1/src/../outside.py",
            "C:\\project-1\\file.py",
            "missing-project/file.py",
            "$CODEX_HOME/file.py",
        ):
            with self.subTest(scope=unsafe), self.assertRaises(LoopError):
                normalize_scope(unsafe, "file", self.project_paths)

    def test_file_locks_allow_parallel_files_and_block_case_equivalent_file(self) -> None:
        self.add_task(
            "FILE-A", "project-1", "critical", lock_mode="file",
            scope=["project-1/src/A.py"],
        )
        self.add_task(
            "FILE-B", "project-1", "high", lock_mode="file",
            scope=["project-1/src/B.py"],
        )
        self.add_task(
            "FILE-A-CASE", "project-1", "medium", lock_mode="file",
            scope=["PROJECT-1\\src\\.\\a.PY"],
        )
        first = self.claim("exec-file-a")
        second = self.claim("exec-file-b")
        conflict = self.claim("exec-file-case")
        self.assertEqual(first["task"]["id"], "FILE-A")
        self.assertEqual(second["task"]["id"], "FILE-B")
        self.assertEqual(conflict["outcome"], "CONFLICT")
        self.assertEqual(conflict["task_id"], "FILE-A-CASE")
        database = connect(self.db_path)
        self.assertEqual(
            database.execute("SELECT status FROM tasks WHERE id='FILE-A-CASE'").fetchone()[0],
            "PENDING",
        )
        database.close()

    def test_module_project_and_file_cross_conflicts_skip_to_other_scope(self) -> None:
        self.add_task(
            "MODULE-SRC", "project-1", "critical", lock_mode="module",
            scope=["project-1/src"],
        )
        self.add_task(
            "FILE-IN-SRC", "project-1", "high", lock_mode="file",
            scope=["project-1/src/inside.py"],
        )
        self.add_task(
            "FILE-IN-DOCS", "project-1", "medium", lock_mode="file",
            scope=["project-1/docs/other.py"],
        )
        self.assertEqual(self.claim("exec-module")["task"]["id"], "MODULE-SRC")
        result = self.claim("exec-docs")
        self.assertEqual(result["task"]["id"], "FILE-IN-DOCS")
        self.assertEqual(result["deferred_conflicts"][0]["task_id"], "FILE-IN-SRC")
        self.assertEqual(
            result["deferred_conflicts"][0]["conflicts"][0]["requested_scope_key"],
            "file:project-1::src/inside.py",
        )
        self.add_task(
            "PROJECT-LOCK", "project-2", "critical", lock_mode="project",
            scope=["project-2/any/file.py"],
        )
        self.add_task(
            "PROJECT-FILE", "project-2", "high", lock_mode="file",
            scope=["project-2/other.py"],
        )
        self.assertEqual(self.claim("exec-project")["task"]["id"], "PROJECT-LOCK")
        self.assertEqual(self.claim("exec-project-file")["outcome"], "CONFLICT")

    def test_multi_lock_conflict_leaves_no_partial_lock(self) -> None:
        self.add_task(
            "LOCK-B", "project-1", "critical", lock_mode="file",
            scope=["project-1/b.py"],
        )
        self.add_task(
            "LOCK-A-B", "project-1", "high", lock_mode="file",
            scope=["project-1/a.py", "project-1/b.py"],
        )
        self.claim("exec-lock-b")
        result = self.claim("exec-lock-a-b")
        self.assertEqual(result["outcome"], "CONFLICT")
        database = connect(self.db_path)
        lock_keys = [row[0] for row in database.execute(
            "SELECT scope_key FROM scope_locks ORDER BY scope_key"
        ).fetchall()]
        database.close()
        self.assertEqual(lock_keys, ["file:project-1::b.py"])

    def test_later_high_priority_task_passes_unclaimed_low_priority_same_scope(self) -> None:
        self.add_task(
            "OLDER-LOW-FILE", "project-1", "low", lock_mode="file",
            scope=["project-1/shared.py"],
        )
        self.add_task(
            "NEWER-HIGH-FILE", "project-1", "high", lock_mode="file",
            scope=["project-1/shared.py"],
        )
        claimed = self.claim("exec-priority-file")
        self.assertEqual(claimed["task"]["id"], "NEWER-HIGH-FILE")

    def test_scope_extension_is_atomic_on_conflict_and_returns_updated_credential(self) -> None:
        self.add_task(
            "EXTEND-A", "local-agent-loop", "critical", lock_mode="file",
            scope=["local-agent-loop/control/a.py"],
        )
        self.add_task(
            "EXTEND-B", "local-agent-loop", "high", lock_mode="file",
            scope=["local-agent-loop/control/b.py"],
        )
        first = self.claim("exec-extend-a")
        self.claim("exec-extend-b")
        report = json.dumps({"scope": ["LOCAL-AGENT-LOOP\\control\\.\\b.py"]})
        refused = self.run_ctl(
            "extend-scope", "exec-extend-a", "EXTEND-A", input_text=report
        )
        self.assertEqual(refused["outcome"], "SCOPE_EXTENSION_CONFLICT")
        self.assertEqual(
            refused["scope_lock_credential"]["scope_keys"],
            ["file:local-agent-loop::control/a.py"],
        )
        database = connect(self.db_path)
        self.assertEqual(
            database.execute("SELECT count(*) FROM task_scopes WHERE task_id='EXTEND-A'").fetchone()[0],
            1,
        )
        database.close()
        self.finish("exec-extend-b", "EXTEND-B")
        extended = self.run_ctl(
            "extend-scope", "exec-extend-a", "EXTEND-A", input_text=report
        )
        self.assertEqual(extended["outcome"], "SCOPE_EXTENDED")
        self.assertEqual(
            extended["scope_lock_credential"]["scope_keys"],
            [
                "file:local-agent-loop::control/a.py",
                "file:local-agent-loop::control/b.py",
            ],
        )
        self.assertEqual(
            first["scope_lock_credential"]["execution_id"], "exec-extend-a"
        )


if __name__ == "__main__":
    unittest.main()
