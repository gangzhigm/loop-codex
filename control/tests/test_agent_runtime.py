from __future__ import annotations

# 中文排查：覆盖 Self-hosted Agent 的 Provider 工厂、工具边界、心跳、超时、重试和 finish。
# 失败先判断属于控制面、模型步骤还是 sandbox；测试中的 ScriptedProvider 用于精确复现步骤序列。
# 任何涉及副作用重试的改动都要核对“写入后不得重放”的断言。

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from _bootstrap import REPOSITORY_ROOT

from runner import agent_runtime
from loop_agent.runtime import sandbox as runtime_sandbox
from loop_agent.runtime.agent import SingleTaskAgent
from loop_agent.runtime.controller import SubprocessLoopController
from loop_agent.runtime.core import (
    ExecutionProfile,
    RuntimeSettings,
    SafeLogger,
    ToolRejected,
    safe_subprocess_environment,
)
from loop_agent.runtime.diagnostics import ProviderDiagnostic, TrustedDiagnosticError
from loop_agent.runtime.protocol import validate_final_result
from loop_agent.runtime.sandbox import ScopePolicy, ToolSandbox
from loopdb import connect, initialize_schema, insert_task, load_initialization_config, now_shanghai
from loop_agent.secrets.store import EnvironmentBackend, SecretStore


class ScriptedProvider:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.timeouts: list[float] = []

    def complete(self, request: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
        self.requests.append(request)
        self.timeouts.append(timeout_seconds)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response(request)
        return response


class FakeController:
    def __init__(self, claim: dict[str, Any]) -> None:
        self.claim_payload = claim
        self.claims: list[tuple[str, str, str, str | None]] = []
        self.heartbeats: list[tuple[str, str]] = []
        self.finishes: list[tuple[str, str, dict[str, Any]]] = []

    def claim(self, execution_id: str, runtime_environment: str, capability_level: str, provider_id: str | None) -> dict[str, Any]:
        self.claims.append((execution_id, runtime_environment, capability_level, provider_id))
        if len(self.claims) > 1:
            raise AssertionError("claimed more than once")
        return self.claim_payload

    def heartbeat(self, execution_id: str, task_id: str) -> dict[str, Any]:
        self.heartbeats.append((execution_id, task_id))
        return {"outcome": "HEARTBEAT"}

    def finish(self, execution_id: str, task_id: str, result: dict[str, Any]) -> dict[str, Any]:
        self.finishes.append((execution_id, task_id, result))
        return {"outcome": "FINISHED", "task_id": task_id, "status": result["status"]}


def tool_call(name: str, arguments: dict[str, Any], call_id: str = "call-1") -> dict[str, Any]:
    return {"type": "tool_calls", "calls": [{"id": call_id, "name": name, "arguments": arguments}]}


def success_result(verification: str = "verified") -> dict[str, Any]:
    return {
        "type": "final",
        "result": {"status": "SUCCEEDED", "summary": "done", "verification": [verification]},
    }


class AgentRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.project = self.workspace / "project"
        self.project.mkdir()
        (self.project / "AGENTS.md").write_text("Use UTF-8.\n", encoding="utf-8")
        (self.project / "file.txt").write_text("before\n", encoding="utf-8")
        self.settings = RuntimeSettings(
            max_steps=4,
            model_timeout_seconds=0.2,
            tool_timeout_seconds=2,
            heartbeat_interval_seconds=60,
            max_file_bytes=20_000,
            max_tool_output_chars=10_000,
        )
        self.config = load_initialization_config()
        self.config["execution_profiles"]["self_hosted_agent"]["providers"]["deepseek"]["capabilities"]["L2"] = {
            "model": "fake-deepseek", "reasoning": "high",
            "attempt_timeout_seconds": 1, "max_retries": 0,
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def task(self, **overrides: Any) -> dict[str, Any]:
        task = {
            "id": "TASK-1",
            "description": "Change the scoped file.",
            "scope": ["project/"],
            "acceptance": ["File contains after."],
            "depends_on": ["READY-1"],
            "runtime_environment": "self_hosted_agent",
            "provider_id": "deepseek",
            "capability_level": "L2",
        }
        task.update(overrides)
        return task

    def test_repository_config_defines_bounded_runtime_settings(self) -> None:
        config = load_initialization_config()
        settings = RuntimeSettings.from_config(config)
        self.assertEqual(settings.max_steps, 24)
        self.assertEqual(settings.model_timeout_seconds, 120)
        self.assertEqual(settings.tool_timeout_seconds, 120)
        self.assertEqual(settings.max_file_bytes, 524_288)
        self.assertEqual(settings.max_tool_output_chars, 50_000)
        self.assertEqual(settings.provider_termination_grace_seconds, 5)
        profile = ExecutionProfile.resolve(
            config, "self_hosted_agent", "deepseek", "L2"
        )
        self.assertEqual(profile.max_retries, 2)

    def test_provider_factory_receives_shared_config_and_secret_store(self) -> None:
        module = types.ModuleType("test_injected_provider")
        captured: dict[str, Any] = {}
        provider = ScriptedProvider([])

        def factory(*, config: dict[str, Any], secret_store: SecretStore) -> ScriptedProvider:
            captured.update({"config": config, "secret_store": secret_store})
            return provider

        module.create_provider = factory
        sys.modules[module.__name__] = module
        store = SecretStore(EnvironmentBackend({}), "Agent Runtime Tests")
        try:
            loaded = agent_runtime.load_provider(
                f"{module.__name__}:create_provider", self.config, store
            )
        finally:
            sys.modules.pop(module.__name__, None)
        self.assertIs(loaded, provider)
        self.assertIs(captured["config"], self.config)
        self.assertIs(captured["secret_store"], store)

    def test_child_process_environment_filters_injected_credentials(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PATH": "safe-path",
                "DEEPSEEK_API_KEY": "test-only-secret",
                "SERVICE_PASSWORD": "test-only-password",
                "NORMAL_SETTING": "visible",
            },
            clear=True,
        ):
            environment = safe_subprocess_environment()
        self.assertEqual(environment["PATH"], "safe-path")
        self.assertEqual(environment["NORMAL_SETTING"], "visible")
        self.assertNotIn("DEEPSEEK_API_KEY", environment)
        self.assertNotIn("SERVICE_PASSWORD", environment)

    def run_agent(self, provider: ScriptedProvider, claim: dict[str, Any] | None = None) -> tuple[dict[str, Any], FakeController]:
        controller = FakeController(claim or {"outcome": "CLAIMED", "task": self.task()})
        agent = SingleTaskAgent(
            provider,
            controller,
            self.workspace,
            self.settings,
            logger=SafeLogger(io.StringIO()),
            config=self.config,
        )
        return agent.run("exec-1", "self_hosted_agent", "L2", "deepseek"), controller

    def test_successful_edit_uses_tool_loop_and_finishes_once(self) -> None:
        provider = ScriptedProvider(
            [
                tool_call("apply_patch", {"path": "project/file.txt", "old": "before", "new": "after"}),
                success_result("file content inspected"),
            ]
        )
        result, controller = self.run_agent(provider)
        self.assertEqual(result["result"]["status"], "SUCCEEDED")
        self.assertEqual((self.project / "file.txt").read_text(encoding="utf-8"), "after\n")
        self.assertEqual(len(controller.claims), 1)
        self.assertEqual(len(controller.finishes), 1)
        self.assertGreaterEqual(len(controller.heartbeats), 4)

    def test_patch_can_create_a_missing_scoped_text_file(self) -> None:
        provider = ScriptedProvider(
            [
                tool_call("apply_patch", {"path": "project/new.txt", "old": "", "new": "created\n"}),
                success_result("new file inspected"),
            ]
        )
        result, _ = self.run_agent(provider)
        self.assertEqual(result["result"]["status"], "SUCCEEDED")
        self.assertEqual((self.project / "new.txt").read_text(encoding="utf-8"), "created\n")

    def test_read_only_task_can_succeed_without_changes(self) -> None:
        provider = ScriptedProvider(
            [tool_call("read_file", {"path": "project/file.txt"}), success_result("read-only evidence")]
        )
        result, _ = self.run_agent(provider)
        self.assertEqual(result["result"]["status"], "SUCCEEDED")
        self.assertEqual((self.project / "file.txt").read_text(encoding="utf-8"), "before\n")
        tool_message = provider.requests[1]["messages"][-1]["content"]["tool_results"][0]
        self.assertTrue(tool_message["ok"])
        self.assertEqual(tool_message["output"]["content"], "before\n")

    def test_scope_escape_and_sensitive_paths_are_refused(self) -> None:
        provider = ScriptedProvider(
            [
                {
                    "type": "tool_calls",
                    "calls": [
                        {"id": "outside", "name": "read_file", "arguments": {"path": "other/file.txt"}},
                        {"id": "parent", "name": "read_file", "arguments": {"path": "project/../other.txt"}},
                        {"id": "secret", "name": "read_file", "arguments": {"path": "project/.env"}},
                        {"id": "env-local", "name": "read_file", "arguments": {"path": "project/.env.local"}},
                        {"id": "git-config", "name": "read_file", "arguments": {"path": "project/.git/config"}},
                    ],
                },
                success_result("refusals inspected"),
            ]
        )
        result, _ = self.run_agent(provider)
        self.assertEqual(result["result"]["status"], "SUCCEEDED")
        refusals = provider.requests[1]["messages"][-1]["content"]["tool_results"]
        self.assertEqual([item["ok"] for item in refusals], [False, False, False, False, False])

    def test_missing_high_risk_approval_becomes_waiting_human(self) -> None:
        provider = ScriptedProvider([tool_call("git_commit", {"message": "commit"})])
        result, controller = self.run_agent(provider)
        self.assertEqual(result["result"]["status"], "WAITING_HUMAN")
        self.assertIn("git_commit", result["result"]["question"])
        self.assertEqual(controller.finishes[0][2]["status"], "WAITING_HUMAN")

    def test_provider_request_timeout_restarts_one_clean_agent_attempt(self) -> None:
        self.config["execution_profiles"]["self_hosted_agent"]["providers"]["deepseek"]["capabilities"]["L2"]["max_retries"] = 1
        provider = ScriptedProvider([TimeoutError("provider timeout"), success_result("continued")])
        result, controller = self.run_agent(provider)
        self.assertEqual(result["result"]["status"], "SUCCEEDED")
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(controller.finishes[0][2]["status"], "SUCCEEDED")

    def test_full_attempt_retry_succeeds_once_and_exhausts_deterministically(self) -> None:
        self.config["execution_profiles"]["self_hosted_agent"]["providers"]["deepseek"]["capabilities"]["L2"]["max_retries"] = 1
        transient = ProviderDiagnostic(
            "server_error", http_status=503, retryable=True, retry_exhausted=True
        )
        provider = ScriptedProvider([TrustedDiagnosticError(transient), success_result("second attempt")])
        result, controller = self.run_agent(provider)
        self.assertEqual(result["result"]["status"], "SUCCEEDED")
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(len(controller.claims), 1)
        self.assertEqual(len(controller.finishes), 1)

        provider = ScriptedProvider([
            TrustedDiagnosticError(transient), TrustedDiagnosticError(transient)
        ])
        result, _ = self.run_agent(provider)
        self.assertEqual(result["result"]["status"], "FAILED")
        self.assertEqual(len(provider.requests), 2)

    def test_trusted_provider_diagnostic_adds_attempt_step_without_private_text(self) -> None:
        private = "private-provider-response-body"
        diagnostic = ProviderDiagnostic(
            "server_error", http_status=503, retryable=True, retry_exhausted=True
        )
        log_stream = io.StringIO()
        provider = ScriptedProvider([TrustedDiagnosticError(diagnostic)])
        controller = FakeController({"outcome": "CLAIMED", "task": self.task()})
        agent = SingleTaskAgent(
            provider, controller, self.workspace, self.settings,
            logger=SafeLogger(log_stream), config=self.config,
        )
        result = agent.run("trusted-diagnostic", "self_hosted_agent", "L2", "deepseek")
        failed = result["result"]
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["diagnostic"], {
            "category": "server_error", "http_status": 503, "retryable": True,
            "retry_exhausted": True, "finish_reason": None, "agent_attempt": 1,
            "model_step": 1,
        })
        self.assertIn("category=server_error", failed["error"])
        self.assertNotIn(private, json.dumps({"result": failed, "log": log_stream.getvalue()}))

    def test_unknown_provider_exception_keeps_only_the_exception_type(self) -> None:
        private = "private-upstream-request-body"
        self.config["execution_profiles"]["self_hosted_agent"]["providers"]["deepseek"]["capabilities"]["L2"]["max_retries"] = 2
        provider = ScriptedProvider([RuntimeError(private), success_result("must not retry")])
        result, _ = self.run_agent(provider)
        failed = result["result"]
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["error"], "runtime error: RuntimeError")
        self.assertNotIn("diagnostic", failed)
        self.assertNotIn(private, json.dumps(failed))
        self.assertEqual(len(provider.requests), 1)

    def test_trusted_authentication_diagnostic_becomes_waiting_human(self) -> None:
        provider = ScriptedProvider([
            TrustedDiagnosticError(ProviderDiagnostic("authentication", http_status=401), requires_human=True)
        ])
        result, controller = self.run_agent(provider)
        waiting = result["result"]
        self.assertEqual(waiting["status"], "WAITING_HUMAN")
        self.assertEqual(waiting["diagnostic"]["category"], "authentication")
        self.assertEqual(waiting["diagnostic"]["agent_attempt"], 1)
        self.assertEqual(waiting["diagnostic"]["model_step"], 1)
        self.assertEqual(controller.finishes[0][2]["status"], "WAITING_HUMAN")

    def test_local_side_effect_suppresses_complete_attempt_retry(self) -> None:
        self.config["execution_profiles"]["self_hosted_agent"]["providers"]["deepseek"]["capabilities"]["L2"]["max_retries"] = 1
        provider = ScriptedProvider([
            tool_call("apply_patch", {"path": "project/file.txt", "old": "before", "new": "after"}),
            TrustedDiagnosticError(ProviderDiagnostic(
                "connection", retryable=True, retry_exhausted=True
            )),
            success_result("must not run"),
        ])
        result, _ = self.run_agent(provider)
        self.assertEqual(result["result"]["status"], "FAILED")
        self.assertIn("local side effect", result["result"]["error"])
        self.assertEqual(len(provider.requests), 2)

    def test_each_retry_receives_a_fresh_complete_attempt_budget(self) -> None:
        profile = self.config["execution_profiles"]["self_hosted_agent"]["providers"]["deepseek"]["capabilities"]["L2"]
        profile.update({"attempt_timeout_seconds": 1, "max_retries": 1})
        self.settings = RuntimeSettings(**{
            **self.settings.__dict__, "heartbeat_interval_seconds": 0.02,
            "model_timeout_seconds": 2, "provider_termination_grace_seconds": 0.2,
        })

        def slow_first(_request: dict[str, Any]) -> dict[str, Any]:
            time.sleep(1.05)
            return success_result()["result"]

        provider = ScriptedProvider([slow_first, success_result("fresh budget")])
        result, controller = self.run_agent(provider)
        self.assertEqual(result["result"]["status"], "SUCCEEDED")
        self.assertEqual(len(provider.requests), 2)
        self.assertGreaterEqual(len(controller.heartbeats), 4)
        self.assertTrue(all(timeout <= 1 for timeout in provider.timeouts))

    def test_heartbeat_failure_is_infrastructure_failure_before_attempt_timeout(self) -> None:
        class FailingHeartbeatController(FakeController):
            def heartbeat(self, execution_id: str, task_id: str) -> dict[str, Any]:
                value = super().heartbeat(execution_id, task_id)
                if len(self.heartbeats) >= 3:
                    raise RuntimeError("heartbeat stalled")
                return value

        self.settings = RuntimeSettings(**{**self.settings.__dict__, "heartbeat_interval_seconds": 0.01})
        provider = ScriptedProvider([lambda _request: (time.sleep(0.05), success_result())[1]])
        controller = FailingHeartbeatController({"outcome": "CLAIMED", "task": self.task()})
        agent = SingleTaskAgent(
            provider, controller, self.workspace, self.settings,
            logger=SafeLogger(io.StringIO()), config=self.config,
        )
        result = agent.run("exec-heartbeat", "self_hosted_agent", "L2", "deepseek")
        self.assertEqual(result["result"]["status"], "FAILED")
        self.assertIn("heartbeat", result["result"]["error"])
        self.assertEqual(len(controller.finishes), 1)

    def test_runtime_config_rejects_attempt_timeout_before_stall_detection(self) -> None:
        config = load_initialization_config()
        config["execution_profiles"]["self_hosted_agent"]["providers"]["deepseek"]["capabilities"]["L1"]["attempt_timeout_seconds"] = 300
        with self.assertRaisesRegex(Exception, "stalled detection"):
            RuntimeSettings.from_config(config)

    def test_process_interruption_becomes_failed_and_is_finished(self) -> None:
        provider = ScriptedProvider([KeyboardInterrupt()])
        result, controller = self.run_agent(provider)
        self.assertEqual(result["result"]["status"], "FAILED")
        self.assertIn("interrupted", result["result"]["error"])
        self.assertEqual(controller.finishes[0][2]["status"], "FAILED")

    def test_invalid_final_result_is_repaired_once_without_agent_retry(self) -> None:
        self.config["execution_profiles"]["self_hosted_agent"]["providers"]["deepseek"]["capabilities"]["L2"]["max_retries"] = 2
        provider = ScriptedProvider([
            {"type": "final", "result": {"status": "SUCCEEDED", "summary": "no proof"}},
            success_result("repaired final"),
        ])
        result, _ = self.run_agent(provider)
        self.assertEqual(result["result"]["status"], "SUCCEEDED")
        self.assertEqual(result["result"]["verification"], ["repaired final"])
        self.assertEqual(len(provider.requests), 2)
        repair = provider.requests[1]
        self.assertTrue(repair["final_repair"])
        self.assertEqual(repair["tools"], [])
        self.assertEqual(
            repair["final_result_schema"]["properties"]["verification"]["type"], "array"
        )

    def test_invalid_final_repair_is_attempted_only_once(self) -> None:
        provider = ScriptedProvider([
            {"type": "final", "result": {"status": "SUCCEEDED", "summary": "no proof"}},
            {"type": "final", "result": {
                "status": "SUCCEEDED", "summary": "still wrong", "verification": "one",
            }},
            success_result("must not be requested"),
        ])
        result, _ = self.run_agent(provider)
        failed = result["result"]
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["diagnostic"]["category"], "final_schema")
        self.assertEqual(failed["diagnostic"]["agent_attempt"], 1)
        self.assertEqual(failed["diagnostic"]["model_step"], 1)
        self.assertEqual(
            failed["diagnostic"]["final_shape"]["allowed_fields"]["verification"]["type"],
            "string",
        )
        self.assertEqual(len(provider.requests), 2)

    def test_invalid_tool_arguments_do_not_restart_agent_attempt(self) -> None:
        self.config["execution_profiles"]["self_hosted_agent"]["providers"]["deepseek"]["capabilities"]["L2"]["max_retries"] = 2
        provider = ScriptedProvider([
            tool_call("read_file", {"path": 42}),
            success_result("must not retry"),
        ])
        result, _ = self.run_agent(provider)
        self.assertEqual(result["result"]["status"], "FAILED")
        self.assertIn("invalid arguments", result["result"]["error"])
        self.assertEqual(len(provider.requests), 1)

    def test_max_steps_exhaustion_is_failed(self) -> None:
        self.settings = RuntimeSettings(**{**self.settings.__dict__, "max_steps": 2})
        self.config["execution_profiles"]["self_hosted_agent"]["providers"]["deepseek"]["capabilities"]["L2"]["max_retries"] = 2
        provider = ScriptedProvider(
            [tool_call("read_file", {"path": "project/file.txt"}, "one"), tool_call("read_file", {"path": "project/file.txt"}, "two")]
        )
        result, _ = self.run_agent(provider)
        self.assertEqual(result["result"]["status"], "FAILED")
        self.assertIn("maximum agent steps", result["result"]["error"])
        self.assertEqual(len(provider.requests), 2)

    def test_attempt_timeout_is_concrete_bounded_failure(self) -> None:
        profile = self.config["execution_profiles"]["self_hosted_agent"]["providers"]["deepseek"]["capabilities"]["L2"]
        profile.update({"attempt_timeout_seconds": 1, "max_retries": 1})
        self.settings = RuntimeSettings(**{
            **self.settings.__dict__, "model_timeout_seconds": 2,
            "provider_termination_grace_seconds": 0.1,
        })

        def exceed_attempt(_request: dict[str, Any]) -> dict[str, Any]:
            time.sleep(1.05)
            return success_result("too late")

        provider = ScriptedProvider([exceed_attempt, exceed_attempt])
        result, _ = self.run_agent(provider)
        self.assertEqual(result["result"]["status"], "FAILED")
        self.assertIn("agent attempt timed out", result["result"]["error"])
        self.assertEqual(len(provider.requests), 2)

    def test_non_claimed_outcome_stops_without_heartbeat_or_finish(self) -> None:
        provider = ScriptedProvider([])
        result, controller = self.run_agent(provider, {"outcome": "NO_TASK"})
        self.assertEqual(result["outcome"], "NO_TASK")
        self.assertEqual(len(controller.claims), 1)
        self.assertFalse(controller.heartbeats)
        self.assertFalse(controller.finishes)
        self.assertFalse(provider.requests)

    def test_routing_mismatch_finishes_as_failed(self) -> None:
        provider = ScriptedProvider([])
        claim = {"outcome": "CLAIMED", "task": self.task(runtime_environment="codex_cli")}
        result, controller = self.run_agent(provider, claim)
        self.assertEqual(result["result"]["status"], "FAILED")
        self.assertEqual(len(controller.finishes), 1)

    def test_context_is_limited_to_claim_fields_and_includes_agents_and_dependency_state(self) -> None:
        provider = ScriptedProvider([success_result()])
        self.run_agent(provider)
        context = provider.requests[0]["messages"][0]["content"]
        self.assertEqual(set(context["task"]), {"id", "description", "scope", "acceptance", "dependencies"})
        self.assertEqual(context["instructions"][0]["path"], "project/AGENTS.md")
        self.assertEqual(context["task"]["dependencies"][0]["state"], "satisfied_before_claim")

    def test_command_tool_denies_interpreters_shell_meta_and_unsafe_git(self) -> None:
        policy = ScopePolicy(self.workspace, ["project/"])
        sandbox = ToolSandbox(policy, self.settings, set())
        with self.assertRaises(ToolRejected):
            sandbox.execute("run_command", {"cwd": "project", "argv": ["python", "test.py"]})
        with self.assertRaises(ToolRejected):
            sandbox.execute("run_command", {"cwd": "project", "argv": ["rg", "x|type .env"]})
        with self.assertRaises(ToolRejected):
            sandbox.execute("run_command", {"cwd": "project", "argv": ["git", "commit", "-m", "x"]})

    def test_command_timeout_is_reported_as_tool_failure(self) -> None:
        policy = ScopePolicy(self.workspace, ["project/"])
        sandbox = ToolSandbox(policy, self.settings, set())
        with patch.object(runtime_sandbox.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 2)):
            with self.assertRaisesRegex(ToolRejected, "timed out"):
                sandbox.execute("run_command", {"cwd": "project", "argv": ["git", "status", "--short"]})

    def test_real_controller_claim_heartbeat_and_finish_round_trip(self) -> None:
        database_path = self.workspace / "loop.sqlite3"
        database = connect(database_path)
        initialize_schema(database)
        insert_task(
            database,
            {
                "id": "ROUND-TRIP",
                "title": "round trip",
                "description": "Read the scoped file.",
                "status": "PENDING",
                "priority": "medium",
                "capability_level": "L2",
                "runtime_environment": "self_hosted_agent",
                "provider_id": "deepseek",
                "created_at": now_shanghai(),
                "scope": ["project/file.txt"],
                "acceptance": ["read verified"],
            },
            actor="test",
            project_paths=["project"],
        )
        database.close()
        provider = ScriptedProvider([success_result("round trip verified")])
        agent = SingleTaskAgent(
            provider,
            SubprocessLoopController(database_path),
            self.workspace,
            self.settings,
            logger=SafeLogger(io.StringIO()),
            config=self.config,
        )
        result = agent.run("round-trip-execution", "self_hosted_agent", "L2", "deepseek")
        database = connect(database_path)
        task = database.execute("SELECT status, result_summary FROM tasks WHERE id='ROUND-TRIP'").fetchone()
        execution = database.execute(
            "SELECT status, outcome FROM executions WHERE execution_id='round-trip-execution'"
        ).fetchone()
        lock_count = database.execute("SELECT count(*) FROM scope_locks").fetchone()[0]
        database.close()
        self.assertEqual(result["outcome"], "FINISHED")
        self.assertEqual((task["status"], task["result_summary"]), ("SUCCEEDED", "done"))
        self.assertEqual((execution["status"], execution["outcome"]), ("FINISHED", "SUCCEEDED"))
        self.assertEqual(lock_count, 0)

    def test_final_contract_rejects_confirmed_and_missing_required_fields(self) -> None:
        with self.assertRaises(Exception):
            validate_final_result({"status": "CONFIRMED", "summary": "not allowed"})
        with self.assertRaises(Exception):
            validate_final_result({"status": "FAILED", "summary": "failed"})
        with self.assertRaises(Exception):
            validate_final_result({"status": "WAITING_HUMAN", "summary": "wait"})


if __name__ == "__main__":
    unittest.main()
