from __future__ import annotations

from _loop_support import *  # noqa: F403


class PlannerPlaceholderTests(LoopTestCase):
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
