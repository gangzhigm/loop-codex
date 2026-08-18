from __future__ import annotations

from _loop_support import *  # noqa: F403


class LoopResultDiagnosticTests(LoopTestCase):
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
