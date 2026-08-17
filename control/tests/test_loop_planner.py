from __future__ import annotations

# 中文排查：Planner 预检、契约、诊断与写回 fencing 回归。
# 公共 fixture 位于 _loop_support.py；业务行为断言保留在各职责模块中。

from _loop_support import *  # noqa: F403


class LoopPlannerTests(LoopTestCase):
    def test_planner_ready_contract_gates_worker_claim_and_preserves_operator_facts(self) -> None:
        self.enqueue_draft("PREFLIGHT-READY")
        before = self.claim("worker-before-ready", "L3")
        self.assertEqual(before["outcome"], "NO_TASK")

        claimed = self.planner_claim("planner-ready")
        self.assertEqual(claimed["outcome"], "CLAIMED")
        self.assertEqual(claimed["execution_kind"], "PLANNER")
        self.assertEqual(
            set(claimed["task"]),
            {
                "id", "title", "status", "preflight_status", "created_at", "updated_at",
                "row_version", "operator_definition",
            },
        )
        self.assertEqual(claimed["client_boundary"]["sandbox"], "read-only")
        self.assertEqual(claimed["client_boundary"]["default_tool_action"], "deny")
        self.assertEqual(
            claimed["client_boundary"]["writeback_transport"],
            "host_controlled_loopctl_stdin",
        )
        self.assertEqual(
            claimed["task"]["operator_definition"]["scope_hint"],
            ["local-agent-loop/control/loopctl.py"],
        )
        self.assertEqual(self.planner_claim("planner-second")["outcome"], "NO_TASK")
        heartbeat = self.run_ctl("preflight-heartbeat", "planner-ready", "PREFLIGHT-READY")
        self.assertGreater(heartbeat["row_version"], claimed["task"]["row_version"])

        ready = self.run_ctl(
            "preflight-ready", "planner-ready", "PREFLIGHT-READY",
            input_text=self.ready_report(),
        )
        self.assertEqual((ready["status"], ready["preflight_status"]), ("PENDING", "READY"))
        repeated = self.run_ctl(
            "preflight-ready", "planner-ready", "PREFLIGHT-READY",
            input_text=self.ready_report(),
        )
        self.assertEqual(repeated["outcome"], "ALREADY_FINISHED")
        state = self.run_ctl("state")
        task = next(item for item in state["tasks"] if item["id"] == "PREFLIGHT-READY")
        self.assertEqual(task["priority"], "critical")
        self.assertEqual(task["runtime_environment"], "self_hosted_agent")
        self.assertEqual(task["provider_id"], "deepseek")
        self.assertEqual(task["capability_level"], "L3")
        self.assertEqual(task["technical_acceptance"], ["运行聚焦回归测试"])
        self.assertEqual(task["preflight_evidence"], ["已核对范围和依赖关系"])
        worker = self.claim("worker-after-ready", "L3")
        self.assertEqual(worker["task"]["id"], "PREFLIGHT-READY")

    def test_planner_ready_rejects_suspicious_question_mark_corruption(self) -> None:
        self.enqueue_draft("PREFLIGHT-UTF8-CORRUPTION")
        self.planner_claim("planner-utf8-corruption")
        report = json.loads(self.ready_report())
        report["technical_acceptance"] = ["???????? metricTotal ????????"]

        rejected = self.run_ctl_error(
            "preflight-ready", "planner-utf8-corruption", "PREFLIGHT-UTF8-CORRUPTION",
            input_text=json.dumps(report, ensure_ascii=False),
        )
        self.assertIn("UTF-8", rejected["message"])

        database = connect(self.db_path)
        task = database.execute(
            "SELECT status, preflight_status FROM tasks WHERE id='PREFLIGHT-UTF8-CORRUPTION'"
        ).fetchone()
        acceptance_count = database.execute(
            "SELECT COUNT(*) FROM task_technical_acceptance WHERE task_id='PREFLIGHT-UTF8-CORRUPTION'"
        ).fetchone()[0]
        database.close()
        self.assertEqual(tuple(task), ("DRAFT", "INSPECTING"))
        self.assertEqual(acceptance_count, 0)

    def test_planner_escalation_requires_operator_approval_and_stdin(self) -> None:
        def approve(task_id: str, *markers: str) -> None:
            patch_path = Path(self.temporary.name) / f"{task_id}-approval.json"
            patch_path.write_text(
                json.dumps(
                    {"description": "Operator business description\n" + "\n".join(markers)},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self.run_ctl("update", task_id, str(patch_path))

        self.enqueue_draft("PREFLIGHT-L5", capability="L5")
        self.planner_claim("planner-l5")
        rejected = self.run_ctl_error(
            "preflight-ready", "planner-l5", "PREFLIGHT-L5",
            input_text=self.ready_report(capability="L5"),
        )
        self.assertIn("L5", rejected["message"])
        database = connect(self.db_path)
        state = database.execute(
            "SELECT status, preflight_status FROM tasks WHERE id='PREFLIGHT-L5'"
        ).fetchone()
        database.close()
        self.assertEqual(tuple(state), ("DRAFT", "INSPECTING"))

        report_path = Path(self.temporary.name) / "planner-result.json"
        report_path.write_text(self.ready_report(), encoding="utf-8")
        rejected_file = self.run_ctl_error(
            "preflight-ready", "planner-l5", "PREFLIGHT-L5", str(report_path)
        )
        self.assertIn("stdin", rejected_file["message"])

        review_report = json.dumps(
            {
                "summary": "operator approval required",
                "question": "Approve the Planner escalation?",
                "options": ["approve", "revise"],
                "split_suggestions": [],
                "evidence": ["static scope inspection completed"],
            }
        )
        self.run_ctl(
            "preflight-needs-review", "planner-l5", "PREFLIGHT-L5",
            input_text=review_report,
        )
        approve("PREFLIGHT-L5", "APPROVED_PLANNER_ESCALATION: L5")
        reclaimed_l5 = self.planner_claim("planner-l5-approved")
        self.assertIn(
            "APPROVED_PLANNER_ESCALATION: L5",
            reclaimed_l5["task"]["operator_definition"]["description"],
        )
        approved_l5 = self.run_ctl(
            "preflight-ready", "planner-l5-approved", "PREFLIGHT-L5",
            input_text=self.ready_report(capability="L5"),
        )
        self.assertEqual(approved_l5["outcome"], "READY")

        self.enqueue_draft("PREFLIGHT-MANUAL", capability="L5", execution_policy="manual")
        self.planner_claim("planner-manual")
        rejected_manual = self.run_ctl_error(
            "preflight-ready", "planner-manual", "PREFLIGHT-MANUAL",
            input_text=self.ready_report(capability="L4"),
        )
        self.assertIn("manual", rejected_manual["message"])
        self.run_ctl(
            "preflight-needs-review", "planner-manual", "PREFLIGHT-MANUAL",
            input_text=review_report,
        )
        approve(
            "PREFLIGHT-MANUAL",
            "APPROVED_PLANNER_ESCALATION: L5",
            "APPROVED_PLANNER_ESCALATION: manual",
        )
        self.planner_claim("planner-manual-approved")
        approved_manual = self.run_ctl(
            "preflight-ready", "planner-manual-approved", "PREFLIGHT-MANUAL",
            input_text=self.ready_report(capability="L5"),
        )
        self.assertEqual(approved_manual["outcome"], "READY")

    def test_planner_ready_normalizes_file_scope_and_rejects_unsafe_scope(self) -> None:
        self.enqueue_draft("PREFLIGHT-FILE")
        self.planner_claim("planner-file")
        ready = self.run_ctl(
            "preflight-ready", "planner-file", "PREFLIGHT-FILE",
            input_text=self.ready_report(
                lock_mode="file",
                scope=["LOCAL-AGENT-LOOP\\control\\.\\LoopCtl.py"],
            ),
        )
        self.assertEqual(ready["outcome"], "READY")
        database = connect(self.db_path)
        stored = database.execute(
            "SELECT scope, scope_key FROM task_scopes WHERE task_id='PREFLIGHT-FILE'"
        ).fetchone()
        database.close()
        self.assertEqual(
            tuple(stored),
            ("local-agent-loop/control/LoopCtl.py", "file:local-agent-loop::control/loopctl.py"),
        )

        self.enqueue_draft("PREFLIGHT-UNSAFE")
        self.planner_claim("planner-unsafe")
        rejected = self.run_ctl_error(
            "preflight-ready", "planner-unsafe", "PREFLIGHT-UNSAFE",
            input_text=self.ready_report(
                lock_mode="file",
                scope=["local-agent-loop/control/../outside.py"],
            ),
        )
        self.assertIn("不安全的 scope", rejected["message"])
        database = connect(self.db_path)
        state = database.execute(
            "SELECT status, preflight_status FROM tasks WHERE id='PREFLIGHT-UNSAFE'"
        ).fetchone()
        database.close()
        self.assertEqual(tuple(state), ("DRAFT", "INSPECTING"))

    def test_planner_needs_review_saves_split_suggestion_without_creating_tasks(self) -> None:
        self.enqueue_draft("PREFLIGHT-REVIEW", capability="L5")
        self.planner_claim("planner-review")
        suggestion = [{
            "reason": "two independently deliverable modules",
            "tasks": [
                {
                    "id": "PROPOSED-A", "title": "module A", "description": "implement module A",
                    "scope": ["local-agent-loop/control/loopdb.py"], "capability_level": "L4",
                    "depends_on": [], "parallel_with": ["PROPOSED-B"],
                },
                {
                    "id": "PROPOSED-B", "title": "module B", "description": "implement module B",
                    "scope": ["local-agent-loop/control/loopctl.py"], "capability_level": "L4",
                    "depends_on": [], "parallel_with": ["PROPOSED-A"],
                },
            ],
        }]
        report = json.dumps(
            {
                "summary": "split decision required",
                "question": "Should the task be split?",
                "options": ["split", "keep atomic"],
                "split_suggestions": suggestion,
                "evidence": ["scope ownership checked"],
            },
            ensure_ascii=False,
        )
        result = self.run_ctl(
            "preflight-needs-review", "planner-review", "PREFLIGHT-REVIEW", input_text=report
        )
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        state = self.run_ctl("state")
        self.assertNotIn("PROPOSED-A", {task["id"] for task in state["tasks"]})
        task = next(item for item in state["tasks"] if item["id"] == "PREFLIGHT-REVIEW")
        self.assertEqual(task["split_suggestions"], suggestion)

        requeued = self.run_ctl("requeue", "PREFLIGHT-REVIEW", "--reason", "keep atomic")
        self.assertEqual((requeued["status"], requeued["preflight_status"]), ("DRAFT", "UNINSPECTED"))
        reclaimed = self.planner_claim("planner-review-second")
        self.assertEqual(reclaimed["task_id"], "PREFLIGHT-REVIEW")

    def test_planner_timeout_requeues_read_only_preflight_and_fences_late_result(self) -> None:
        self.enqueue_draft("PREFLIGHT-TIMEOUT")
        self.planner_claim("planner-old")
        database = connect(self.db_path)
        database.execute(
            "UPDATE preflight_executions SET heartbeat_at='2000-01-01T00:00:00.000+08:00', "
            "lease_expires_at='2000-01-01T00:00:00.000+08:00', "
            "attempt_deadline_at='2000-01-01T00:00:00.000+08:00' WHERE execution_id='planner-old'"
        )
        database.close()
        reclaimed = self.planner_claim("planner-new")
        self.assertEqual(reclaimed["task_id"], "PREFLIGHT-TIMEOUT")
        self.assertEqual(reclaimed["recovered"], ["planner-old"])
        late = self.run_ctl_error(
            "preflight-ready", "planner-old", "PREFLIGHT-TIMEOUT", input_text=self.ready_report()
        )
        self.assertIn("迟到结果被拒绝", late["message"])
        database = connect(self.db_path)
        old = database.execute(
            "SELECT status, outcome, recovery_action FROM preflight_executions WHERE execution_id='planner-old'"
        ).fetchone()
        database.close()
        self.assertEqual(tuple(old), ("TIMED_OUT", "TIMED_OUT", "requeue"))

    def test_preflight_failed_requires_operator_recheck(self) -> None:
        self.enqueue_draft("PREFLIGHT-FAILED")
        self.planner_claim("planner-failed")
        result = self.run_ctl(
            "preflight-fail", "planner-failed", "PREFLIGHT-FAILED",
            input_text=json.dumps(
                {"summary": "static check failed", "error": "scope is ambiguous", "evidence": ["two roots match"]}
            ),
        )
        self.assertEqual((result["status"], result["preflight_status"]), ("NEEDS_REVIEW", "FAILED"))
        self.assertEqual(self.claim("worker-skips-failed", "L3")["outcome"], "NO_TASK")

    def test_finish_round_trips_safe_diagnostic_and_requeue_clears_it(self) -> None:
        self.add_task("DIAGNOSTIC-ROUNDTRIP", "project-1")
        self.claim("diagnostic-execution")
        canonical_diagnostic = self.result_diagnostic()
        diagnostic = json.loads(json.dumps(canonical_diagnostic))
        fields = diagnostic["final_shape"]["allowed_fields"]
        diagnostic["final_shape"]["allowed_fields"] = dict(reversed(list(fields.items())))
        self.run_ctl(
            "finish",
            "diagnostic-execution",
            "DIAGNOSTIC-ROUNDTRIP",
            input_text=json.dumps(
                {
                    "status": "FAILED",
                    "summary": "Provider final result was invalid.",
                    "error": "provider diagnostic: category=final_schema",
                    "diagnostic": diagnostic,
                }
            ),
        )

        database = connect(self.db_path)
        task = all_tasks(database)[0]
        stored = database.execute(
            "SELECT result_diagnostic_json FROM tasks WHERE id='DIAGNOSTIC-ROUNDTRIP'"
        ).fetchone()[0]
        database.close()
        self.assertEqual(task["result"]["diagnostic"], canonical_diagnostic)
        self.assertEqual(json.loads(stored), canonical_diagnostic)

        self.run_ctl("requeue", "DIAGNOSTIC-ROUNDTRIP", "--reason", "retry safely")
        database = connect(self.db_path)
        cleared = database.execute(
            "SELECT result_diagnostic_json FROM tasks WHERE id='DIAGNOSTIC-ROUNDTRIP'"
        ).fetchone()[0]
        database.close()
        self.assertIsNone(cleared)

    def test_finish_rejects_untrusted_diagnostic_fields_without_persisting_values(self) -> None:
        self.add_task("DIAGNOSTIC-REJECT", "project-1")
        self.claim("diagnostic-reject-execution")
        canary = "credential-value-must-not-persist"
        diagnostic = {**self.result_diagnostic(), "raw_response": canary}
        error = self.run_ctl_error(
            "finish",
            "diagnostic-reject-execution",
            "DIAGNOSTIC-REJECT",
            input_text=json.dumps(
                {
                    "status": "FAILED",
                    "summary": "failed",
                    "error": "safe error",
                    "diagnostic": diagnostic,
                }
            ),
        )
        self.assertIn("包含未知字段", error["message"])
        self.assertNotIn(canary, json.dumps(error, ensure_ascii=False))
        success_error = self.run_ctl_error(
            "finish",
            "diagnostic-reject-execution",
            "DIAGNOSTIC-REJECT",
            input_text=json.dumps(
                {
                    "status": "SUCCEEDED",
                    "summary": "done",
                    "verification": ["checked"],
                    "diagnostic": self.result_diagnostic(),
                }
            ),
        )
        self.assertIn("SUCCEEDED 不得包含 result diagnostic", success_error["message"])
        database = connect(self.db_path)
        row = database.execute(
            "SELECT status, result_diagnostic_json FROM tasks WHERE id='DIAGNOSTIC-REJECT'"
        ).fetchone()
        database.close()
        self.assertEqual(tuple(row), ("RUNNING", None))

    def test_database_validation_rejects_noncanonical_diagnostic_json(self) -> None:
        self.add_task("DIAGNOSTIC-INVALID-STORED", "project-1")
        database = connect(self.db_path)
        database.execute(
            "UPDATE tasks SET result_diagnostic_json=? WHERE id='DIAGNOSTIC-INVALID-STORED'",
            ('{"category":"final_schema","raw_response":"forbidden"}',),
        )
        validation = validate_database(database)
        database.close()
        self.assertFalse(validation["ok"])
        self.assertIn("任务结果诊断无效: DIAGNOSTIC-INVALID-STORED", validation["errors"])


if __name__ == "__main__":
    unittest.main()
