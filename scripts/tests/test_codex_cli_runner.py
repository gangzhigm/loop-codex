from __future__ import annotations

# 中文排查：使用 FAKE_CODEX 子进程覆盖 Runner 的 JSONL、超时、scope、脱敏和真实 finish。
# 失败先查看 FAKE_CODEX_MODE、记录文件和 Runner 事件，再判断是进程层还是控制面层。
# 测试创建的子进程必须在用例结束前退出，避免影响后续并发和端口检查。

import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True

from _bootstrap import REPOSITORY_ROOT

from runner import codex_cli_runner
from runner.agent_runtime import ExecutionProfile, SafeLogger
from runner.codex_cli_runner import (
    CodexCliRunner,
    CodexCliRunnerError,
    CodexCliSettings,
)
from loopdb import connect, initialize_schema, insert_task, load_initialization_config, now_shanghai


FAKE_CODEX = r'''from __future__ import annotations
import json
import os
import sys
import time
from pathlib import Path

mode = os.environ.get("FAKE_CODEX_MODE", "success")
record = os.environ.get("FAKE_CODEX_RECORD")
pid_file = os.environ.get("FAKE_CODEX_PID")
counter_file = os.environ.get("FAKE_CODEX_COUNTER")
prompt = sys.stdin.read()
if record:
    Path(record).write_text(json.dumps({"argv": sys.argv[1:], "stdin": prompt}, ensure_ascii=False), encoding="utf-8")
if pid_file:
    Path(pid_file).write_text(str(os.getpid()), encoding="utf-8")
attempt = 1
if counter_file:
    counter = Path(counter_file)
    attempt = int(counter.read_text(encoding="utf-8")) + 1 if counter.exists() else 1
    counter.write_text(str(attempt), encoding="utf-8")

result = {
    "status": "SUCCEEDED",
    "summary": "fake completed",
    "verification": ["fake verification"],
    "completed": ["fake edit"],
    "error": None,
    "question": None,
    "options": [],
    "next_step": None,
    "percent": None,
}

if mode == "auth":
    print("Unauthorized 401 Authorization=Bearer-secret sk-testsecret123 C:\\Users\\Admin\\.codex\\auth.json", file=sys.stderr)
    raise SystemExit(1)
if mode == "nonzero":
    print("internal fake failure", file=sys.stderr)
    raise SystemExit(7)
if mode == "fail_once" and attempt == 1:
    print("transient fake failure", file=sys.stderr)
    raise SystemExit(7)
if mode == "side_effect_failure":
    print(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "text": "changed"}}))
    print("failure after possible side effect", file=sys.stderr)
    raise SystemExit(7)
if mode == "timeout":
    time.sleep(30)
if mode == "delay":
    time.sleep(0.25)
if mode == "no_final":
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}))
    raise SystemExit(0)
if mode == "invalid_final":
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "not-json"}}))
    raise SystemExit(0)
if mode == "large_noise":
    print("x" * 20000)

print(json.dumps({"type": "thread.started", "thread_id": "fake-thread"}))
print("non-json noise")
print(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "text": "ignore"}}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(result, ensure_ascii=False)}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}))
'''


class FakeController:
    def __init__(self, claim: dict[str, object]) -> None:
        self.claim_payload = claim
        self.claims: list[tuple[str, str, str, str | None]] = []
        self.heartbeats: list[tuple[str, str]] = []
        self.finishes: list[tuple[str, str, dict[str, object]]] = []

    def claim(self, execution_id: str, runtime_environment: str, capability_level: str, provider_id: str | None) -> dict[str, object]:
        self.claims.append((execution_id, runtime_environment, capability_level, provider_id))
        if len(self.claims) > 1:
            raise AssertionError("claim called more than once")
        return self.claim_payload

    def heartbeat(self, execution_id: str, task_id: str) -> dict[str, str]:
        self.heartbeats.append((execution_id, task_id))
        return {"outcome": "HEARTBEAT"}

    def finish(self, execution_id: str, task_id: str, result: dict[str, object]) -> dict[str, str]:
        self.finishes.append((execution_id, task_id, result))
        return {"outcome": "FINISHED", "task_id": task_id, "status": str(result["status"])}


class CodexCliRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.project = self.workspace / "project-one"
        self.other_project = self.workspace / "project-two"
        self.project.mkdir(parents=True)
        self.other_project.mkdir()
        (self.project / ".git").mkdir()
        (self.project / "AGENTS.md").write_text("Use UTF-8.\n", encoding="utf-8")
        (self.project / "file.txt").write_text("before\n", encoding="utf-8")
        (self.other_project / "other.txt").write_text("other\n", encoding="utf-8")
        self.registry = self.workspace / "registry.md"
        self.registry.write_text(
            "| 文件夹名 | 这个文件夹是什么 |\n| --- | --- |\n"
            "| `project-one` | test project |\n| `project-two` | second project |\n",
            encoding="utf-8",
        )
        self.prompt = self.root / "cli-worker.md"
        self.prompt.write_text("CLI authoritative prompt. Read AGENTS.md and preserve Git changes.\n", encoding="utf-8")
        self.fake_codex = self.root / "fake_codex.py"
        self.fake_codex.write_text(FAKE_CODEX, encoding="utf-8")
        self.record = self.root / "record.json"
        self.pid_file = self.root / "pid.txt"
        self.counter_file = self.root / "counter.txt"
        self.config = load_initialization_config()
        self.config["workspace"] = {
            **self.config["workspace"],
            "task_root": str(self.workspace),
            "project_registry": str(self.registry),
        }
        self.config["execution_profiles"]["codex_cli"]["capabilities"]["L2"] = {
            "model": "fake-model", "reasoning": "high",
            "attempt_timeout_seconds": 2, "max_retries": 0,
        }
        self.settings = CodexCliSettings(
            command_prefix=(sys.executable, str(self.fake_codex)),
            prompt_path=self.prompt,
            use_user_config=True,
            sandbox="workspace-write",
            termination_grace_seconds=2,
            heartbeat_interval_seconds=0.05,
            stalled_after_seconds=0.1,
            max_stdout_chars=4096,
            max_stderr_chars=4096,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def task(self, **overrides: object) -> dict[str, object]:
        task: dict[str, object] = {
            "id": "CLI-TASK-1",
            "description": "Edit the scoped file.",
            "scope": ["project-one/file.txt"],
            "acceptance": ["File is verified."],
            "depends_on": ["READY-1"],
            "runtime_environment": "codex_cli",
            "provider_id": None,
            "capability_level": "L2",
        }
        task.update(overrides)
        return task

    def run_runner(
        self,
        mode: str = "success",
        *,
        claim: dict[str, object] | None = None,
        settings: CodexCliSettings | None = None,
    ) -> tuple[dict[str, object], FakeController]:
        controller = FakeController(claim or {"outcome": "CLAIMED", "task": self.task()})
        runner = CodexCliRunner(
            controller,
            self.config,
            settings or self.settings,
            logger=SafeLogger(io.StringIO()),
        )
        environment = {
            "FAKE_CODEX_MODE": mode,
            "FAKE_CODEX_RECORD": str(self.record),
            "FAKE_CODEX_PID": str(self.pid_file),
            "FAKE_CODEX_COUNTER": str(self.counter_file),
        }
        with patch.dict(os.environ, environment, clear=False):
            result = runner.run("codex-cli-l2-test", "L2")
        return result, controller

    def test_repository_config_defines_non_sensitive_profile_mapping(self) -> None:
        settings = CodexCliSettings.from_config(load_initialization_config())
        self.assertEqual(settings.sandbox, "workspace-write")
        self.assertEqual(settings.max_stdout_chars, 1_000_000)
        self.assertTrue(settings.use_user_config)
        self.assertEqual(
            ExecutionProfile.resolve(load_initialization_config(), "codex_cli", None, "L5").model,
            "gpt-5.6-sol",
        )
        serialized = json.dumps(load_initialization_config()["codex_cli"], ensure_ascii=False).lower()
        self.assertNotIn("token", serialized)
        self.assertNotIn("credential", serialized)
        self.assertNotIn("api_key", serialized)

    def test_success_uses_required_safe_cli_arguments_and_structured_result(self) -> None:
        result, controller = self.run_runner()
        self.assertEqual(result["result"]["status"], "SUCCEEDED")
        self.assertEqual(len(controller.claims), 1)
        self.assertEqual(controller.claims[0][1:], ("codex_cli", "L2", None))
        self.assertEqual(len(controller.finishes), 1)
        self.assertGreaterEqual(len(controller.heartbeats), 2)
        record = json.loads(self.record.read_text(encoding="utf-8"))
        arguments = record["argv"]
        for required in ["exec", "--json", "--ephemeral", "--cd", "--output-schema"]:
            self.assertIn(required, arguments)
        self.assertNotIn("--ignore-user-config", arguments)
        self.assertIn("workspace-write", arguments)
        self.assertIn("fake-model", arguments)
        self.assertIn('model_reasoning_effort="high"', arguments)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", arguments)
        self.assertNotIn("--dangerously-bypass-hook-trust", arguments)
        self.assertNotIn("--add-dir", arguments)
        self.assertEqual(Path(arguments[arguments.index("--cd") + 1]), self.project)
        self.assertIn("CLI authoritative prompt", record["stdin"])
        self.assertIn('"scope": [', record["stdin"])
        self.assertIn("project-one/file.txt", record["stdin"])

    def test_user_config_switch_only_changes_the_isolation_flag(self) -> None:
        settings = CodexCliSettings(**{**self.settings.__dict__, "use_user_config": False})
        result, _ = self.run_runner(settings=settings)
        self.assertEqual(result["result"]["status"], "SUCCEEDED")
        arguments = json.loads(self.record.read_text(encoding="utf-8"))["argv"]
        self.assertIn("--ignore-user-config", arguments)
        for required in ["--json", "--ephemeral", "--model", "--sandbox", "--cd", "--output-schema"]:
            self.assertIn(required, arguments)

    def test_user_config_switch_must_be_boolean(self) -> None:
        config = load_initialization_config()
        config["codex_cli"] = {**config["codex_cli"], "use_user_config": "yes"}
        with self.assertRaisesRegex(CodexCliRunnerError, "use_user_config"):
            CodexCliSettings.from_config(config)

    def test_jsonl_noise_and_large_output_keep_final_agent_message(self) -> None:
        result, _ = self.run_runner("large_noise")
        self.assertEqual(result["result"]["status"], "SUCCEEDED")

    def test_missing_or_invalid_final_result_is_failed_and_finished(self) -> None:
        for mode in ["no_final", "invalid_final"]:
            with self.subTest(mode=mode):
                result, controller = self.run_runner(mode)
                self.assertEqual(result["result"]["status"], "FAILED")
                self.assertIn("no valid final result", result["result"]["error"])
                self.assertEqual(len(controller.finishes), 1)

    def test_authentication_failure_waits_for_human_and_redacts_sensitive_output(self) -> None:
        self.config["execution_profiles"]["codex_cli"]["capabilities"]["L2"]["max_retries"] = 1
        result, controller = self.run_runner("auth")
        final = result["result"]
        self.assertEqual(final["status"], "WAITING_HUMAN")
        serialized = json.dumps(final, ensure_ascii=False).lower()
        self.assertNotIn("sk-testsecret", serialized)
        self.assertNotIn("authorization=", serialized)
        self.assertNotIn(".codex", serialized)
        self.assertEqual(len(controller.finishes), 1)
        self.assertEqual(self.counter_file.read_text(encoding="utf-8"), "1")

    def test_nonzero_exit_is_failed_and_finished(self) -> None:
        result, controller = self.run_runner("nonzero")
        self.assertEqual(result["result"]["status"], "FAILED")
        self.assertIn("code 7", result["result"]["error"])
        self.assertEqual(len(controller.finishes), 1)

    def test_retry_uses_new_attempt_budget_and_finishes_once(self) -> None:
        self.config["execution_profiles"]["codex_cli"]["capabilities"]["L2"]["max_retries"] = 1
        result, controller = self.run_runner("fail_once")
        self.assertEqual(result["result"]["status"], "SUCCEEDED")
        self.assertEqual(self.counter_file.read_text(encoding="utf-8"), "2")
        self.assertEqual(len(controller.claims), 1)
        self.assertEqual(len(controller.finishes), 1)

    def test_retry_exhaustion_and_possible_side_effect_suppression(self) -> None:
        self.config["execution_profiles"]["codex_cli"]["capabilities"]["L2"]["max_retries"] = 1
        result, _ = self.run_runner("nonzero")
        self.assertEqual(result["result"]["status"], "FAILED")
        self.assertEqual(self.counter_file.read_text(encoding="utf-8"), "2")
        self.counter_file.unlink()
        result, _ = self.run_runner("side_effect_failure")
        self.assertEqual(result["result"]["status"], "FAILED")
        self.assertIn("side effects", result["result"]["error"])
        self.assertEqual(self.counter_file.read_text(encoding="utf-8"), "1")

    def test_execution_profile_is_an_immutable_pre_claim_snapshot(self) -> None:
        profile = ExecutionProfile.resolve(self.config, "codex_cli", None, "L2")
        self.config["execution_profiles"]["codex_cli"]["capabilities"]["L2"]["model"] = "changed"
        self.assertEqual((profile.model, profile.reasoning), ("fake-model", "high"))

    def test_config_requires_stall_detection_before_attempt_timeout(self) -> None:
        config = load_initialization_config()
        config["execution_profiles"]["codex_cli"]["capabilities"]["L1"]["attempt_timeout_seconds"] = 300
        with self.assertRaisesRegex(CodexCliRunnerError, "stalled detection"):
            CodexCliSettings.from_config(config, command_prefix=(sys.executable,))

    def test_timeout_terminates_process_and_finishes(self) -> None:
        self.config["execution_profiles"]["codex_cli"]["capabilities"]["L2"]["attempt_timeout_seconds"] = 1
        result, controller = self.run_runner("timeout")
        self.assertEqual(result["result"]["status"], "FAILED")
        self.assertIn("timed out", result["result"]["error"])
        self.assertEqual(len(controller.finishes), 1)
        pid = int(self.pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.05)
        else:
            self.fail("fake Codex CLI process survived timeout cleanup")

    def test_heartbeat_runs_while_cli_is_active(self) -> None:
        result, controller = self.run_runner("delay")
        self.assertEqual(result["result"]["status"], "SUCCEEDED")
        self.assertGreaterEqual(len(controller.heartbeats), 4)

    def test_non_claimed_outcomes_do_not_start_codex_or_finish(self) -> None:
        for outcome in ["NO_TASK", "SLOT_FULL", "CONFLICT"]:
            with self.subTest(outcome=outcome):
                self.record.unlink(missing_ok=True)
                result, controller = self.run_runner(claim={"outcome": outcome})
                self.assertEqual(result["outcome"], outcome)
                self.assertEqual(len(controller.claims), 1)
                self.assertFalse(controller.heartbeats)
                self.assertFalse(controller.finishes)
                self.assertFalse(self.record.exists())

    def test_routing_mismatch_finishes_failed_without_starting_codex(self) -> None:
        claim = {"outcome": "CLAIMED", "task": self.task(runtime_environment="self_hosted_agent")}
        result, controller = self.run_runner(claim=claim)
        self.assertEqual(result["result"]["status"], "FAILED")
        self.assertEqual(len(controller.finishes), 1)
        self.assertFalse(self.record.exists())

    def test_multi_project_external_and_unsafe_scopes_wait_for_human(self) -> None:
        scopes = [
            ["project-one/file.txt", "project-two/other.txt"],
            ["OSS:bucket/file.txt"],
            ["project-one/../project-two/other.txt"],
            ["missing-project/file.txt"],
        ]
        for value in scopes:
            with self.subTest(scope=value):
                self.record.unlink(missing_ok=True)
                claim = {"outcome": "CLAIMED", "task": self.task(scope=value)}
                result, controller = self.run_runner(claim=claim)
                self.assertEqual(result["result"]["status"], "WAITING_HUMAN")
                self.assertEqual(len(controller.finishes), 1)
                self.assertFalse(self.record.exists())

    def test_real_loop_controller_round_trip_releases_scope_lock(self) -> None:
        database_path = self.root / "loop.sqlite3"
        database = connect(database_path)
        initialize_schema(database)
        insert_task(
            database,
            {
                "id": "CLI-ROUND-TRIP",
                "title": "Codex CLI round trip",
                "description": "Use the fake CLI.",
                "status": "PENDING",
                "priority": "medium",
                "execution_profile": "standard",
                "runtime_environment": "codex_cli",
                "created_at": now_shanghai(),
                "scope": ["project-one/file.txt"],
                "acceptance": ["fake verification"],
            },
            actor="test",
            project_paths=["project-one", "project-two"],
        )
        database.close()
        controller = codex_cli_runner.SubprocessLoopController(database_path)
        runner = CodexCliRunner(controller, self.config, self.settings, logger=SafeLogger(io.StringIO()))
        with patch.dict(
            os.environ,
            {"FAKE_CODEX_MODE": "success", "FAKE_CODEX_RECORD": str(self.record)},
            clear=False,
        ):
            result = runner.run("codex-cli-round-trip", "L2")
        self.assertEqual(result["result"]["status"], "SUCCEEDED")
        database = connect(database_path)
        task = database.execute("SELECT status FROM tasks WHERE id='CLI-ROUND-TRIP'").fetchone()
        execution = database.execute(
            "SELECT status, outcome FROM executions WHERE execution_id='codex-cli-round-trip'"
        ).fetchone()
        locks = database.execute("SELECT count(*) FROM scope_locks").fetchone()[0]
        database.close()
        self.assertEqual(task["status"], "SUCCEEDED")
        self.assertEqual((execution["status"], execution["outcome"]), ("FINISHED", "SUCCEEDED"))
        self.assertEqual(locks, 0)


if __name__ == "__main__":
    unittest.main()
