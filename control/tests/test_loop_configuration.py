from __future__ import annotations

# 中文排查：配置、初始化、状态投影与执行路由回归。
# 公共 fixture 位于 _loop_support.py；业务行为断言保留在各职责模块中。

from _loop_support import *  # noqa: F403


class LoopConfigurationTests(LoopTestCase):
    def test_initialization_config_owns_deployment_settings(self) -> None:
        config = load_initialization_config()
        self.assertEqual(config["config_version"], "4.4.0")
        self.assertEqual(config["prompts"]["operator"], "operator/operator.md")
        self.assertEqual(config["prompts"]["planner"], "planner/planner.md")
        self.assertEqual(config["prompts"]["worker"], "worker/worker.md")
        self.assertNotIn("automations", config)
        self.assertEqual(config["health"]["scheduler"], "windows_task_scheduler")
        self.assertEqual(config["health"]["interval_minutes"], 30)
        self.assertEqual(config["dashboard"]["port"], 4178)
        self.assertEqual(config["health"]["failure_threshold"], 3)
        self.assertEqual(config["priority_policy"]["levels"], ["blocker", "critical", "high", "medium", "low"])
        self.assertEqual(set(config["runtime_environments"]), set(CANONICAL_RUNTIME_ENVIRONMENTS))
        self.assertEqual(config["task_execution"]["global_max_active_executions"], 8)
        self.assertEqual(
            config["task_execution"]["platform_max_active_executions"],
            {"codex_cli": 5, "self_hosted_agent": 5},
        )
        self.assertNotIn("profile_parallel_limits", config["task_execution"])
        self.assertNotIn("capability_parallel_limits", config["task_execution"])
        self.assertEqual(config["planner"]["execution_kind"], "PLANNER")
        self.assertEqual(config["planner"]["default_runtime_environment"], "codex_cli")
        self.assertGreaterEqual(config["planner"]["attempt_timeout_seconds"], config["planner"]["lease_seconds"])
        self.assertEqual(
            config["planner"]["client_boundary"],
            {
                "sandbox": "read-only",
                "approval_policy": "never",
                "network_access": False,
                "default_tool_action": "deny",
                "source_access": "read-only",
                "writeback": {
                    "transport": "host_controlled_loopctl_stdin",
                    "payload_encoding": "utf-8",
                    "integrity_policy": "reject_suspicious_question_mark_corruption",
                    "controller": str(LOOPCTL),
                    "allowed_commands": [
                        "preflight-claim", "preflight-heartbeat", "preflight-ready",
                        "preflight-needs-review", "preflight-fail",
                    ],
                    "direct_sql": False,
                    "report_files": False,
                },
            },
        )
        self.assertEqual(
            config["planner"]["scheduler"],
            {"scheduled": True, "interval_minutes": 5, "heartbeat_interval_seconds": 15},
        )
        self.assertEqual(set(config["execution_profiles"]), set(CANONICAL_RUNTIME_ENVIRONMENTS))
        self.assertEqual(
            set(config["execution_profiles"]["codex_cli"]["capabilities"]), set(CAPABILITY_LEVELS)
        )
        self.assertEqual(
            set(config["execution_profiles"]["self_hosted_agent"]["providers"]["deepseek"]["capabilities"]),
            set(CAPABILITY_LEVELS),
        )
        self.assertEqual(
            {
                config["execution_profiles"]["self_hosted_agent"]["providers"]["deepseek"]
                ["capabilities"][level]["model"]
                for level in CAPABILITY_LEVELS
            },
            {"deepseek-v4-flash", "deepseek-v4-pro"},
        )

    def test_initialization_check_validates_planner_and_workers(self) -> None:
        completed = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                str(BASE_DIR / "control" / "deployment_checks" / "check-initialization.ps1"), "-SkipCodexCliCheck",
            ],
            cwd=BASE_DIR,
            text=True,
            encoding="utf-8",
            errors="strict",
            capture_output=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout.lstrip("\ufeff"))
        self.assertEqual(result["outcome"], "VALID")
        self.assertGreaterEqual(result["checks"], 40)
        self.assertEqual(len(result["operator_actions"]), 4)

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
        self.assertEqual(result["schema_version"], "3.7.0")

    def test_fresh_schema_has_capability_routing_and_execution_snapshot(self) -> None:
        database = connect(self.db_path)
        columns = {row[1] for row in database.execute("PRAGMA table_info(tasks)").fetchall()}
        execution_columns = {row[1] for row in database.execute("PRAGMA table_info(executions)").fetchall()}
        preflight_columns = {
            row[1] for row in database.execute("PRAGMA table_info(preflight_executions)").fetchall()
        }
        version = database.execute("PRAGMA user_version").fetchone()[0]
        database.close()
        self.assertIn("archived_at", columns)
        self.assertNotIn("execution_profile", columns)
        self.assertTrue({"capability_level", "provider_id", "execution_policy"}.issubset(columns))
        self.assertIn("runtime_environment", columns)
        self.assertIn("result_diagnostic_json", columns)
        self.assertTrue(
            {
                "estimated_capability_level", "preflight_status", "preflight_execution_id",
                "scope_hint_json", "lock_mode", "split_suggestions_json",
            }.issubset(columns)
        )
        self.assertTrue(
            {
                "runtime_environment", "provider_id", "capability_level", "execution_policy",
                "model", "reasoning", "attempt_timeout_seconds", "max_retries",
            }.issubset(execution_columns)
        )
        self.assertIn("execution_kind", execution_columns)
        self.assertTrue(
            {"execution_kind", "attempt_deadline_at", "claimed_task_row_version", "recovery_action"}
            .issubset(preflight_columns)
        )
        self.assertEqual(version, SCHEMA_USER_VERSION)

    def test_enqueue_and_update_runtime_environment_are_exposed_by_state(self) -> None:
        task_path = Path(self.temporary.name) / "runtime-task.json"
        task_path.write_text(
            json.dumps(
                {
                    "id": "RUNTIME-UPDATE",
                    "title": "runtime update",
                    "description": "test",
                    "priority": "medium",
                    "estimated_capability_level": "L2",
                    "runtime_environment": "codex_cli",
                    "scope": ["local-agent-loop/control/loopctl.py"],
                    "acceptance": ["test"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.run_ctl("enqueue", str(task_path))
        patch_path = Path(self.temporary.name) / "runtime-patch.json"
        patch_path.write_text(
            '{"runtime_environment":"self_hosted_agent","provider_id":"deepseek"}',
            encoding="utf-8",
        )
        self.run_ctl("update", "RUNTIME-UPDATE", str(patch_path))
        state = self.run_ctl("state")
        task = next(item for item in state["tasks"] if item["id"] == "RUNTIME-UPDATE")
        self.assertEqual(task["runtime_environment"], "self_hosted_agent")
        self.assertEqual(task["provider_id"], "deepseek")

        patch_path.write_text('{"runtime_environment":"unknown"}', encoding="utf-8")
        error = self.run_ctl_error("update", "RUNTIME-UPDATE", str(patch_path))
        self.assertIn("运行环境无效", error["message"])

    def test_enqueue_uses_configured_default_runtime_environment(self) -> None:
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
        result = self.run_ctl("enqueue", str(task_path))
        self.assertEqual(result["outcome"], "ENQUEUED")
        state = self.run_ctl("state")
        task = next(item for item in state["tasks"] if item["id"] == "MISSING-RUNTIME")
        self.assertEqual(task["runtime_environment"], "codex_cli")
        self.assertEqual((task["status"], task["preflight_status"]), ("DRAFT", "UNINSPECTED"))

    def test_provider_is_required_only_for_self_hosted_agent(self) -> None:
        task_path = Path(self.temporary.name) / "provider-task.json"
        base = {
            "id": "PROVIDER-RULE", "title": "provider", "capability_level": "L2",
            "scope": ["local-agent-loop/control/loopctl.py"], "acceptance": ["test"],
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


if __name__ == "__main__":
    unittest.main()
