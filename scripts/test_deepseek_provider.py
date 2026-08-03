from __future__ import annotations

import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agent_runtime import RuntimeSettings, SafeLogger, SingleTaskAgent, ToolSandbox
from deepseek_provider import DeepSeekProvider, DeepSeekProviderError, DeepSeekSettings


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


def tool_response() -> tuple[int, dict[str, Any]]:
    return 200, {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
        {"id": "call-1", "type": "function", "function": {"name": "apply_patch", "arguments": json.dumps({"path": "project/file.txt", "old": "before", "new": "after"})}}
    ]}}]}


def final_response() -> tuple[int, dict[str, Any]]:
    result = {"status": "SUCCEEDED", "summary": "patched", "verification": ["local fake server completed"]}
    return 200, {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]}


class DeepSeekProviderTests(unittest.TestCase):
    def settings(self, base_url: str) -> DeepSeekSettings:
        return DeepSeekSettings(
            api_base_url=base_url,
            model="deepseek-v4-flash",
            timeout_seconds=2,
            max_retries=2,
            retry_backoff_seconds=0.01,
            max_retry_backoff_seconds=0.05,
            api_key_environment_variable="DEEPSEEK_API_KEY",
            supported_execution_profiles=("standard",),
        )

    def test_missing_external_key_fails_without_value(self) -> None:
        config = {"deepseek": {**self.settings("https://api.deepseek.com").__dict__}}
        provider = DeepSeekProvider.from_config(config, environment={})
        with self.assertRaisesRegex(DeepSeekProviderError, "external injection"):
            provider.complete({"protocol_version": "1.0", "credential_access_approved": True, "messages": [{"role": "runtime", "content": {}}], "tools": ToolSandbox.TOOL_SCHEMAS}, 2)

    def test_credential_is_not_accessed_without_task_approval(self) -> None:
        provider = DeepSeekProvider(self.settings("https://api.deepseek.com"), {"DEEPSEEK_API_KEY": "test-only-token"})
        with self.assertRaisesRegex(DeepSeekProviderError, "explicit task approval"):
            provider.complete({"protocol_version": "1.0", "messages": [{"role": "runtime", "content": {}}], "tools": ToolSandbox.TOOL_SCHEMAS}, 2)

    def test_local_fake_service_runs_tool_loop_and_finishes(self) -> None:
        with TemporaryDirectory() as temporary, LocalServer([tool_response(), final_response()]) as server:
            workspace = Path(temporary)
            project = workspace / "project"
            project.mkdir()
            (project / "AGENTS.md").write_text("Use UTF-8.\n", encoding="utf-8")
            (project / "file.txt").write_text("before\n", encoding="utf-8")
            task = {"id": "DEEPSEEK-TEST", "description": "Patch the scoped file. APPROVED_ACTIONS: credential_access", "scope": ["project/"], "acceptance": ["File is updated."], "depends_on": [], "runtime_environment": "deepseek", "execution_profile": "standard"}
            controller = FakeController(task)
            provider = DeepSeekProvider(self.settings(server.url), {"DEEPSEEK_API_KEY": "test-only-token"}, sleeper=lambda _: None)
            agent = SingleTaskAgent(provider, controller, workspace, RuntimeSettings(4, 2, 2, 60, 20_000, 10_000), SafeLogger(io.StringIO()))
            result = agent.run("test-execution", "deepseek", "standard")

            self.assertEqual(result["result"]["status"], "SUCCEEDED")
            self.assertEqual((project / "file.txt").read_text(encoding="utf-8"), "after\n")
            self.assertEqual(controller.result and controller.result["status"], "SUCCEEDED")
            self.assertGreaterEqual(controller.heartbeats, 4)
            self.assertEqual([item["path"] for item in ScriptedHandler.requests], ["/chat/completions", "/chat/completions"])
            self.assertTrue(all(item["authorization_present"] for item in ScriptedHandler.requests))
            self.assertEqual(ScriptedHandler.requests[0]["body"]["tools"][0]["type"], "function")
            self.assertIn("final-result contract", ScriptedHandler.requests[0]["body"]["messages"][0]["content"])
            self.assertEqual(ScriptedHandler.requests[1]["body"]["messages"][-1]["role"], "tool")

    def test_retry_is_bounded_for_429_and_5xx(self) -> None:
        with LocalServer([(429, {}), (500, {}), final_response()]) as server:
            waits: list[float] = []
            provider = DeepSeekProvider(self.settings(server.url), {"DEEPSEEK_API_KEY": "test-only-token"}, sleeper=waits.append)
            response = provider.complete({"protocol_version": "1.0", "credential_access_approved": True, "messages": [{"role": "runtime", "content": {}}], "tools": ToolSandbox.TOOL_SCHEMAS}, 2)
            self.assertEqual(response["type"], "final")
            self.assertEqual(waits, [0.01, 0.02])
            self.assertEqual(len(ScriptedHandler.requests), 3)

    def test_auth_failure_is_not_retried(self) -> None:
        with LocalServer([(401, {})]) as server:
            provider = DeepSeekProvider(self.settings(server.url), {"DEEPSEEK_API_KEY": "test-only-token"}, sleeper=lambda _: self.fail("should not retry"))
            with self.assertRaisesRegex(DeepSeekProviderError, "HTTP 401"):
                provider.complete({"protocol_version": "1.0", "credential_access_approved": True, "messages": [{"role": "runtime", "content": {}}], "tools": ToolSandbox.TOOL_SCHEMAS}, 2)
            self.assertEqual(len(ScriptedHandler.requests), 1)

    def test_startup_rejects_other_environment_and_unsupported_profile(self) -> None:
        provider = DeepSeekProvider(self.settings("https://api.deepseek.com"), {"DEEPSEEK_API_KEY": "test-only-token"})
        with self.assertRaisesRegex(DeepSeekProviderError, "runtime environment"):
            provider.validate_startup("codex_cli", "standard")
        with self.assertRaisesRegex(DeepSeekProviderError, "execution profile"):
            provider.validate_startup("deepseek", "advanced")


if __name__ == "__main__":
    unittest.main()
