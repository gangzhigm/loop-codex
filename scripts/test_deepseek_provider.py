from __future__ import annotations

import io
import json
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import Mock

from agent_runtime import ExecutionProfile, RuntimeSettings, SafeLogger, SingleTaskAgent, ToolSandbox
from deepseek_provider import (
    DeepSeekProvider,
    DeepSeekProviderError,
    DeepSeekSettings,
    verify_deepseek_credential,
)
from loopdb import CAPABILITY_LEVELS, load_initialization_config
from secret_store import EnvironmentBackend, SecretStore


class ScriptedHandler(BaseHTTPRequestHandler):
    responses: list[tuple[int, dict[str, Any]]] = []
    requests: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802 - HTTP handler API
        length = int(self.headers.get("Content-Length", "0"))
        self.__class__.requests.append({
            "path": self.path,
            "authorization_present": bool(self.headers.get("Authorization")),
            "body": json.loads(self.rfile.read(length).decode("utf-8")),
        })
        status, payload = self.__class__.responses.pop(0)
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: Any) -> None:
        pass


class LocalServer:
    def __init__(self, responses: list[tuple[int, dict[str, Any]]]) -> None:
        ScriptedHandler.responses = list(responses)
        ScriptedHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ScriptedHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "LocalServer":
        self.thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"


class FakeController:
    def __init__(self, task: dict[str, Any]) -> None:
        self.task = task
        self.heartbeats = 0
        self.result: dict[str, Any] | None = None

    def claim(self, *_: str) -> dict[str, Any]:
        return {"outcome": "CLAIMED", "task": self.task}

    def heartbeat(self, *_: str) -> dict[str, Any]:
        self.heartbeats += 1
        return {"outcome": "HEARTBEAT"}

    def finish(self, _execution_id: str, task_id: str, result: dict[str, Any]) -> dict[str, Any]:
        self.result = result
        return {"outcome": "FINISHED", "task_id": task_id, "status": result["status"]}


def tool_response(
    name: str = "apply_patch",
    arguments: dict[str, Any] | None = None,
    call_id: str = "call-1",
) -> tuple[int, dict[str, Any]]:
    if arguments is None:
        arguments = {"path": "project/file.txt", "old": "before", "new": "after"}
    return 200, {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
    ]}}]}


def final_response() -> tuple[int, dict[str, Any]]:
    result = {"status": "SUCCEEDED", "summary": "patched", "verification": ["local fake server completed"]}
    return 200, {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]}


class DeepSeekProviderTests(unittest.TestCase):
    @staticmethod
    def store(environment: dict[str, str]) -> SecretStore:
        return SecretStore(EnvironmentBackend(environment), "DeepSeek Provider Tests")

    def settings(self, base_url: str) -> DeepSeekSettings:
        profiles = {
            level: (
                "deepseek-v4-flash" if level in {"L1", "L2"} else "deepseek-v4-pro",
                {"L1": "low", "L2": "high", "L3": "low", "L4": "high", "L5": "xhigh"}[level],
            )
            for level in CAPABILITY_LEVELS
        }
        return DeepSeekSettings(
            api_base_url=base_url,
            timeout_seconds=2,
            request_max_retries=2,
            retry_backoff_seconds=0.01,
            max_retry_backoff_seconds=0.05,
            secret_ref="DEEPSEEK_API_KEY",
            capability_profiles=profiles,
        )

    def request(self, capability_level: str = "L2") -> dict[str, Any]:
        model, reasoning = self.settings("https://api.deepseek.com").capability_profiles[capability_level]
        return {
            "protocol_version": "1.0",
            "credential_access_approved": True,
            "messages": [{"role": "runtime", "content": {}}],
            "tools": ToolSandbox.TOOL_SCHEMAS,
            "execution_profile": {
                "runtime_environment": "self_hosted_agent", "provider_id": "deepseek",
                "capability_level": capability_level, "model": model, "reasoning": reasoning,
            },
        }

    def run_local_agent(
        self,
        server: LocalServer,
        *,
        request_max_retries: int = 2,
        agent_max_retries: int = 2,
        max_steps: int = 4,
    ) -> tuple[dict[str, Any], FakeController, str]:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "project"
            project.mkdir()
            (project / "AGENTS.md").write_text("Use UTF-8.\n", encoding="utf-8")
            (project / "file.txt").write_text("before\n", encoding="utf-8")
            task = {
                "id": "DEEPSEEK-READ-TEST",
                "description": "Read the scoped file. APPROVED_ACTIONS: credential_access",
                "scope": ["project/"],
                "acceptance": ["Read-only evidence is returned."],
                "depends_on": [],
                "runtime_environment": "self_hosted_agent",
                "provider_id": "deepseek",
                "capability_level": "L2",
            }
            config = load_initialization_config()
            config["execution_profiles"]["self_hosted_agent"]["providers"]["deepseek"]["capabilities"]["L2"]["max_retries"] = agent_max_retries
            log_stream = io.StringIO()
            logger = SafeLogger(log_stream)
            provider = DeepSeekProvider(
                replace(self.settings(server.url), request_max_retries=request_max_retries),
                self.store({"DEEPSEEK_API_KEY": "test-only-token"}),
                sleeper=lambda _: None,
                logger=logger,
            )
            controller = FakeController(task)
            agent = SingleTaskAgent(
                provider,
                controller,
                workspace,
                RuntimeSettings(max_steps, 2, 2, 60, 20_000, 10_000),
                logger,
                config=config,
            )
            result = agent.run("test-execution", "self_hosted_agent", "L2", "deepseek")
            return result, controller, log_stream.getvalue()

    def test_missing_external_key_fails_without_value(self) -> None:
        config = load_initialization_config()
        config["secret_management"]["backend"] = "environment"
        provider = DeepSeekProvider.from_config(config, environment={})
        with self.assertRaises(DeepSeekProviderError) as captured:
            provider.complete(self.request(), 2)
        self.assertEqual(captured.exception.diagnostic.category, "authentication")

    def test_credential_is_not_accessed_without_task_approval(self) -> None:
        store = Mock()
        provider = DeepSeekProvider(self.settings("https://api.deepseek.com"), store)
        with self.assertRaises(DeepSeekProviderError) as captured:
            request = self.request()
            request.pop("credential_access_approved")
            provider.complete(request, 2)
        self.assertEqual(captured.exception.diagnostic.category, "local_protocol")
        store.get.assert_not_called()

    def test_local_fake_service_runs_tool_loop_and_finishes(self) -> None:
        with TemporaryDirectory() as temporary, LocalServer([tool_response(), final_response()]) as server:
            token = "local-provider-test-token"
            log_stream = io.StringIO()
            workspace = Path(temporary)
            project = workspace / "project"
            project.mkdir()
            (project / "AGENTS.md").write_text("Use UTF-8.\n", encoding="utf-8")
            (project / "file.txt").write_text("before\n", encoding="utf-8")
            task = {"id": "DEEPSEEK-TEST", "description": "Patch the scoped file. APPROVED_ACTIONS: credential_access", "scope": ["project/"], "acceptance": ["File is updated."], "depends_on": [], "runtime_environment": "self_hosted_agent", "provider_id": "deepseek", "capability_level": "L2"}
            controller = FakeController(task)
            provider = DeepSeekProvider(
                self.settings(server.url),
                self.store({"DEEPSEEK_API_KEY": token}),
                sleeper=lambda _: None,
            )
            agent = SingleTaskAgent(provider, controller, workspace, RuntimeSettings(4, 2, 2, 60, 20_000, 10_000), SafeLogger(log_stream), config=load_initialization_config())
            result = agent.run("test-execution", "self_hosted_agent", "L2", "deepseek")

            self.assertEqual(result["result"]["status"], "SUCCEEDED")
            self.assertEqual((project / "file.txt").read_text(encoding="utf-8"), "after\n")
            self.assertEqual(controller.result and controller.result["status"], "SUCCEEDED")
            self.assertGreaterEqual(controller.heartbeats, 4)
            self.assertEqual([item["path"] for item in ScriptedHandler.requests], ["/chat/completions", "/chat/completions"])
            self.assertTrue(all(item["authorization_present"] for item in ScriptedHandler.requests))
            self.assertEqual(ScriptedHandler.requests[0]["body"]["tools"][0]["type"], "function")
            self.assertEqual(ScriptedHandler.requests[0]["body"]["model"], "deepseek-v4-flash")
            self.assertEqual(ScriptedHandler.requests[0]["body"]["thinking"], {"type": "enabled"})
            self.assertEqual(ScriptedHandler.requests[0]["body"]["response_format"], {"type": "json_object"})
            self.assertIn("final-result contract", ScriptedHandler.requests[0]["body"]["messages"][0]["content"])
            self.assertEqual(ScriptedHandler.requests[1]["body"]["messages"][-1]["role"], "tool")
            persisted_surface = json.dumps(
                {"result": result, "finish": controller.result, "requests": ScriptedHandler.requests},
                ensure_ascii=False,
            ) + log_stream.getvalue()
            self.assertNotIn(token, persisted_surface)

    def test_local_fake_service_preserves_repeated_read_only_tool_sequence(self) -> None:
        responses = [
            tool_response("read_file", {"path": "project/file.txt"}, "read-1"),
            tool_response("read_file", {"path": "project/file.txt"}, "read-2"),
            final_response(),
        ]
        with LocalServer(responses) as server:
            result, controller, _log = self.run_local_agent(server)

        self.assertEqual(result["result"]["status"], "SUCCEEDED")
        self.assertEqual(controller.result and controller.result["status"], "SUCCEEDED")
        self.assertEqual(len(ScriptedHandler.requests), 3)
        role_sequences = [
            [message["role"] for message in request["body"]["messages"]]
            for request in ScriptedHandler.requests
        ]
        self.assertEqual(role_sequences[0], ["system", "system"])
        self.assertEqual(role_sequences[1][-2:], ["assistant", "tool"])
        self.assertEqual(role_sequences[2][-4:], ["assistant", "tool", "assistant", "tool"])
        self.assertEqual(ScriptedHandler.requests[1]["body"]["messages"][-1]["tool_call_id"], "read-1")
        self.assertEqual(ScriptedHandler.requests[2]["body"]["messages"][-1]["tool_call_id"], "read-2")

    def test_retry_is_bounded_for_429_and_5xx(self) -> None:
        with LocalServer([(429, {}), (500, {}), final_response()]) as server:
            waits: list[float] = []
            provider = DeepSeekProvider(
                self.settings(server.url),
                self.store({"DEEPSEEK_API_KEY": "test-only-token"}),
                sleeper=waits.append,
            )
            response = provider.complete(self.request(), 2)
            self.assertEqual(response["type"], "final")
            self.assertEqual(waits, [0.01, 0.02])
            self.assertEqual(len(ScriptedHandler.requests), 3)

    def test_429_recovers_inside_provider_request_budget_without_agent_retry(self) -> None:
        with LocalServer([(429, {}), final_response()]) as server:
            result, _controller, log = self.run_local_agent(server)

        self.assertEqual(result["result"]["status"], "SUCCEEDED")
        self.assertEqual(len(ScriptedHandler.requests), 2)
        self.assertEqual(log.count('"event": "provider_request_retry"'), 1)
        self.assertNotIn('"event": "agent_attempt_retry"', log)

    def test_5xx_exhaustion_uses_bounded_provider_and_agent_retry_layers(self) -> None:
        with LocalServer([(503, {})] * 9) as server:
            result, _controller, log = self.run_local_agent(server)

        self.assertEqual(result["result"]["status"], "FAILED")
        self.assertEqual(result["result"]["diagnostic"]["category"], "server_error")
        self.assertEqual(result["result"]["diagnostic"]["agent_attempt"], 3)
        self.assertEqual(len(ScriptedHandler.requests), 9)
        self.assertEqual(log.count('"event": "provider_request_retry"'), 6)
        self.assertEqual(log.count('"event": "provider_request_retries_exhausted"'), 3)
        self.assertEqual(log.count('"event": "agent_attempt_retry"'), 2)

    def test_deterministic_provider_failures_do_not_restart_agent_attempt(self) -> None:
        invalid_tool = (200, {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
            {"id": "bad", "type": "function", "function": {"name": "read_file", "arguments": "not-json"}}
        ]}}]})
        cases = [
            ((400, {}), "request_invalid"),
            ((200, {"choices": [{"finish_reason": "stop", "message": {"content": "not-json"}}]}), "invalid_final_json"),
            ((200, {"choices": [{"finish_reason": "length", "message": {}}]}), "truncated_response"),
            (invalid_tool, "invalid_tool_call"),
        ]
        for failure, category in cases:
            with self.subTest(category=category), LocalServer([failure, final_response()]) as server:
                result, _controller, log = self.run_local_agent(server)
            self.assertEqual(result["result"]["status"], "FAILED")
            self.assertEqual(result["result"]["diagnostic"]["category"], category)
            self.assertEqual(len(ScriptedHandler.requests), 1)
            self.assertNotIn('"event": "agent_attempt_retry"', log)

    def test_auth_failure_is_not_retried(self) -> None:
        token = "auth-failure-test-token"
        with LocalServer([(401, {})]) as server:
            provider = DeepSeekProvider(
                self.settings(server.url),
                self.store({"DEEPSEEK_API_KEY": token}),
                sleeper=lambda _: self.fail("should not retry"),
            )
            with self.assertRaises(DeepSeekProviderError) as captured:
                provider.complete(self.request(), 2)
            self.assertEqual(len(ScriptedHandler.requests), 1)
        self.assertNotIn(token, str(captured.exception))
        self.assertIsNone(captured.exception.__cause__)
        self.assertEqual(captured.exception.diagnostic.as_dict(), {
            "category": "authentication", "http_status": 401, "retryable": False,
            "retry_exhausted": False, "finish_reason": None, "agent_attempt": None,
            "model_step": None,
        })

    def test_connection_verification_uses_one_local_request_without_exposing_value(self) -> None:
        token = "connection-test-token"
        with LocalServer([final_response()]) as server:
            self.assertTrue(verify_deepseek_credential(token, self.settings(server.url)))
        self.assertEqual(len(ScriptedHandler.requests), 1)
        self.assertTrue(ScriptedHandler.requests[0]["authorization_present"])
        self.assertEqual(ScriptedHandler.requests[0]["body"]["max_tokens"], 1)
        self.assertNotIn(token, json.dumps(ScriptedHandler.requests[0]["body"]))

    def test_startup_rejects_other_environment_and_unsupported_profile(self) -> None:
        provider = DeepSeekProvider(
            self.settings("https://api.deepseek.com"),
            self.store({"DEEPSEEK_API_KEY": "test-only-token"}),
        )
        with self.assertRaises(DeepSeekProviderError) as captured:
            provider.validate_startup(ExecutionProfile("codex_cli", None, "L2", "x", "high", 600, 0))
        self.assertEqual(captured.exception.diagnostic.category, "local_protocol")
        with self.assertRaises(DeepSeekProviderError) as captured:
            provider.validate_startup(ExecutionProfile("self_hosted_agent", "deepseek", "L2", "wrong", "high", 600, 0))
        self.assertEqual(captured.exception.diagnostic.category, "local_protocol")

    def test_repository_profiles_cover_flash_pro_and_thinking_modes(self) -> None:
        settings = DeepSeekSettings.from_config(load_initialization_config())
        self.assertEqual(settings.request_max_retries, 2)
        self.assertEqual(settings.capability_profiles["L1"], ("deepseek-v4-flash", "low"))
        self.assertEqual(settings.capability_profiles["L2"], ("deepseek-v4-flash", "high"))
        self.assertEqual(settings.capability_profiles["L3"], ("deepseek-v4-pro", "low"))
        self.assertEqual(settings.capability_profiles["L5"], ("deepseek-v4-pro", "xhigh"))

    def test_pro_non_thinking_profile_is_sent_to_api(self) -> None:
        with LocalServer([final_response()]) as server:
            provider = DeepSeekProvider(
                self.settings(server.url),
                self.store({"DEEPSEEK_API_KEY": "test-only-token"}),
            )
            response = provider.complete(self.request("L3"), 2)
        self.assertEqual(response["type"], "final")
        self.assertEqual(ScriptedHandler.requests[0]["body"]["model"], "deepseek-v4-pro")
        self.assertEqual(ScriptedHandler.requests[0]["body"]["thinking"], {"type": "disabled"})

    def test_http_diagnostics_use_only_allowed_metadata_and_retry_semantics(self) -> None:
        cases = [
            (400, "request_invalid", False, False),
            (401, "authentication", False, False),
            (403, "authentication", False, False),
            (429, "rate_limited", True, True),
            (500, "server_error", True, True),
            (503, "server_error", True, True),
        ]
        for status, category, retryable, retry_exhausted in cases:
            with self.subTest(status=status), LocalServer([(status, {})]) as server:
                provider = DeepSeekProvider(
                    replace(self.settings(server.url), request_max_retries=0),
                    self.store({"DEEPSEEK_API_KEY": "test-only-token"}),
                    sleeper=lambda _: self.fail("retries must be bounded"),
                )
                with self.assertRaises(DeepSeekProviderError) as captured:
                    provider.complete(self.request(), 2)
            diagnostic = captured.exception.diagnostic
            self.assertEqual(diagnostic.category, category)
            self.assertEqual(diagnostic.http_status, status)
            self.assertEqual(diagnostic.retryable, retryable)
            self.assertEqual(diagnostic.retry_exhausted, retry_exhausted)
            self.assertNotIn("test-only-token", str(captured.exception))

    def test_connection_timeout_and_response_diagnostics_are_sanitized(self) -> None:
        error_cases = [
            (Mock(side_effect=TimeoutError("private timeout body")), "request_timeout", True),
            (Mock(side_effect=OSError("private connection body")), "connection", True),
        ]
        for opener, category, retryable in error_cases:
            with self.subTest(category=category):
                provider = DeepSeekProvider(
                    replace(self.settings("https://example.invalid"), request_max_retries=0),
                    self.store({"DEEPSEEK_API_KEY": "test-only-token"}), opener=opener,
                )
                with self.assertRaises(DeepSeekProviderError) as captured:
                    provider.complete(self.request(), 2)
            self.assertEqual(captured.exception.diagnostic.category, category)
            self.assertEqual(captured.exception.diagnostic.retryable, retryable)
            self.assertTrue(captured.exception.diagnostic.retry_exhausted)
            self.assertNotIn("private", str(captured.exception))

        responses = [
            (b"", "empty_or_malformed_response", None),
            (json.dumps({"choices": [{"finish_reason": "length", "message": {}}]}).encode(), "truncated_response", "length"),
            (json.dumps({"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": []}}]}).encode(), "invalid_tool_call", "tool_calls"),
            (json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": "not-json-private"}}]}).encode(), "invalid_final_json", "stop"),
        ]
        for raw, category, finish_reason in responses:
            with self.subTest(category=category):
                with self.assertRaises(DeepSeekProviderError) as captured:
                    DeepSeekProvider._normalize_response(raw)
            self.assertEqual(captured.exception.diagnostic.category, category)
            self.assertEqual(captured.exception.diagnostic.finish_reason, finish_reason)
            self.assertNotIn("private", str(captured.exception))


if __name__ == "__main__":
    unittest.main()
