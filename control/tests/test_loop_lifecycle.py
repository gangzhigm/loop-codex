from __future__ import annotations

# 中文排查：人工确认、阻塞答复、归档、依赖与重新排队回归。
# 公共 fixture 位于 _loop_support.py；业务行为断言保留在各职责模块中。

from _loop_support import *  # noqa: F403


class LoopLifecycleTests(LoopTestCase):
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
        self.assertIn("只有等待人工的任务", error["message"])

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
        row_version = database.execute(
            "SELECT row_version FROM tasks WHERE id=?", (task_id,)
        ).fetchone()[0]
        database.close()
        unarchived = self.run_ctl(
            "unarchive", task_id, "--reason", "return to current view",
            "--expected-row-version", str(row_version),
        )
        self.assertEqual(unarchived["outcome"], "UNARCHIVED")
        database = connect(self.db_path)
        history_after_unarchive = database.execute(
            "SELECT count(*) FROM task_history WHERE task_id=?", (task_id,)
        ).fetchone()[0]
        row_version = database.execute(
            "SELECT row_version FROM tasks WHERE id=?", (task_id,)
        ).fetchone()[0]
        database.close()
        repeated_unarchive = self.run_ctl(
            "unarchive", task_id, "--reason", "must not duplicate",
            "--expected-row-version", str(row_version),
        )
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
        statuses = [
            "DRAFT", "NEEDS_REVIEW", "PENDING", "RUNNING", "WAITING_CONFLICT",
            "WAITING_HUMAN", "SUCCEEDED",
        ]
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

    def test_draft_requeue_returns_to_uninspected_instead_of_bypassing_planner(self) -> None:
        self.add_task("DRAFT-TASK", "project-1")
        database = connect(self.db_path)
        database.execute("UPDATE tasks SET status='DRAFT' WHERE id='DRAFT-TASK'")
        database.close()
        result = self.run_ctl("requeue", "DRAFT-TASK", "--reason", "人工需求已确认")
        self.assertEqual(result["outcome"], "REQUEUED")
        database = connect(self.db_path)
        status = database.execute(
            "SELECT status, preflight_status FROM tasks WHERE id='DRAFT-TASK'"
        ).fetchone()
        database.close()
        self.assertEqual(tuple(status), ("DRAFT", "UNINSPECTED"))


if __name__ == "__main__":
    unittest.main()
