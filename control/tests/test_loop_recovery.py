from __future__ import annotations

# 中文排查：心跳、租约、超时、隔离与 Runner 恢复回归。
# 公共 fixture 位于 _loop_support.py；业务行为断言保留在各职责模块中。

from _loop_support import *  # noqa: F403


class LoopRecoveryTests(LoopTestCase):
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
        self.assertEqual(result["outcome"], "NO_TASK")
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

    def test_quarantined_module_blocks_descendant_file_without_consuming_capacity(self) -> None:
        self.add_task(
            "QUARANTINED-MODULE", "project-1", "critical", lock_mode="module",
            scope=["project-1/src"],
        )
        self.add_task(
            "DESCENDANT-FILE", "project-1", "high", lock_mode="file",
            scope=["project-1/src/child.py"],
        )
        self.add_task(
            "OTHER-FILE", "project-2", "medium", lock_mode="file",
            scope=["project-2/other.py"],
        )
        self.claim("exec-quarantined-module")
        database = connect(self.db_path)
        database.execute(
            "UPDATE executions SET heartbeat_at='2000-01-01T00:00:00+08:00', "
            "lease_expires_at='2999-01-01T00:00:00+08:00' "
            "WHERE execution_id='exec-quarantined-module'"
        )
        database.close()

        claimed = self.claim("exec-other-after-quarantine")

        self.assertEqual(claimed["task"]["id"], "OTHER-FILE")
        conflict = claimed["deferred_conflicts"][0]["conflicts"][0]
        self.assertEqual(conflict["blocker_lock_status"], "QUARANTINED")
        database = connect(self.db_path)
        descendant_status = database.execute(
            "SELECT status FROM tasks WHERE id='DESCENDANT-FILE'"
        ).fetchone()[0]
        execution_status = database.execute(
            "SELECT status FROM executions WHERE execution_id='exec-quarantined-module'"
        ).fetchone()[0]
        lock_status = database.execute(
            "SELECT status FROM scope_locks WHERE execution_id='exec-quarantined-module'"
        ).fetchone()[0]
        active = database.execute(
            "SELECT count(*) FROM executions WHERE status='RUNNING'"
        ).fetchone()[0]
        database.close()
        self.assertEqual(descendant_status, "PENDING")
        self.assertEqual(execution_status, "STALLED")
        self.assertEqual(lock_status, "QUARANTINED")
        self.assertEqual(active, 1)

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

        self.assertEqual(result["outcome"], "NO_TASK")
        recovery = result["recovery_required"][0]
        self.assertTrue(recovery["lease_expired"])
        self.assertFalse(recovery["heartbeat_stalled"])
        self.assertFalse(recovery["attempt_timed_out"])

    def test_runner_stall_requires_termination_confirmation_and_never_duplicates_scope(self) -> None:
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
        self.assertEqual(result["recovery_required"][0]["recovery_confirmation"], "runner_confirmed_terminated")
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
        self.assertIn("受控 Runner 确认旧进程树已终止", human_state["human_question"])
        database.close()
        recovered = self.run_ctl("recover", "exec-stalled", "--runner-confirmed-terminated")
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
        self.assertEqual(statuses["CONFLICT-1"], "PENDING")
        self.assertEqual(statuses["CONFLICT-2"], "PENDING")
        recovered = self.run_ctl("recover", "exec-blocker", "--runner-confirmed-terminated")
        self.assertEqual(recovered["requeued_conflicts"], [])

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
            "recover", "exec-recovery-actions", "--runner-confirmed-terminated", "--action", "wait"
        )
        self.assertEqual(waiting["outcome"], "WAITING")
        repeated_wait = self.run_ctl(
            "recover", "exec-recovery-actions", "--runner-confirmed-terminated", "--action", "wait"
        )
        self.assertEqual(repeated_wait["outcome"], "ALREADY_WAITING")
        failed = self.run_ctl(
            "recover", "exec-recovery-actions", "--runner-confirmed-terminated", "--action", "failed"
        )
        self.assertEqual(failed["task_status"], "FAILED")
        repeated_failed = self.run_ctl(
            "recover", "exec-recovery-actions", "--runner-confirmed-terminated", "--action", "failed"
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
            "recover", "exec-fenced-old", "--runner-confirmed-terminated", "--action", "requeue"
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


if __name__ == "__main__":
    unittest.main()
