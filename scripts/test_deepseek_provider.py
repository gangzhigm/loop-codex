from __future__ import annotations

import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agent_runtime import ExecutionProfile, RuntimeSettings, SafeLogger, SingleTaskAgent, ToolSandbox
from deepseek_provider import DeepSeekProvider, DeepSeekProviderError, DeepSeekSettings
from loopdb import CAPABILITY_LEVELS, load_initialization_config


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
            max_retries=2,
            retry_backoff_seconds=0.01,
            max_retry_backoff_seconds=0.05,
            api_key_environment_variable="DEEPSEEK_API_KEY",
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

    def test_missing_external_key_fails_without_value(self) -> None:
        config = load_initialization_config()
        provider = DeepSeekProvider.from_config(config, environment={})
        with self.assertRaisesRegex(DeepSeekProviderError, "external injection"):
            provider.complete(self.request(), 2)

    def test_credential_is_not_accessed_without_task_approval(self) -> None:
        provider = DeepSeekProvider(self.settings("https://api.deepseek.com"), {"DEEPSEEK_API_KEY": "test-only-token"})
        with self.assertRaisesRegex(DeepSeekProviderError, "explicit task approval"):
            request = self.request()
            request.pop("credential_access_approved")
            provider.complete(request, 2)

    def test_local_fake_service_runs_tool_loop_and_finishes(self) -> None:
        with TemporaryDirectory() as temporary, LocalServer([tool_response(), final_response()]) as server:
            workspace = Path(temporary)
            project = workspace / "project"
            project.mkdir()
            (project / "AGENTS.md").write_text("Use UTF-8.\n", encoding="utf-8")
            (project / "file.txt").write_text("before\n", encoding="utf-8")
            task = {"id": "DEEPSEEK-TEST", "description": "Patch the scoped file. APPROVED_ACTIONS: credential_access", "scope": ["project/"], "acceptance": ["File is updated."], "depends_on": [], "runtime_environment": "self_hosted_agent", "provider_id": "deepseek", "capability_level": "L2"}
            controller = FakeController(task)
            provider = DeepSeekProvider(self.settings(server.url), {"DEEPSEEK_API_KEY": "test-only-token"}, sleeper=lambda _: None)
            agent = SingleTaskAgent(provider, controller, workspace, RuntimeSettings(4, 2, 2, 60, 20_000, 10_000), SafeLogger(io.StringIO()), config=load_initialization_config())
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
            self.assertIn("final-result contract", ScriptedHandler.requests[0]["body"]["messages"][0]["content"])
            self.assertEqual(ScriptedHandler.requests[1]["body"]["messages"][-1]["role"], "tool")

    def test_retry_is_bounded_for_429_and_5xx(self) -> None:
        with LocalServer([(429, {}), (500, {}), final_response()]) as server:
            waits: list[float] = []
            provider = DeepSeekProvider(self.settings(server.url), {"DEEPSEEK_API_KEY": "test-only-token"}, sleeper=waits.append)
            response = provider.complete(self.request(), 2)
            self.assertEqual(response["type"], "final")
            self.assertEqual(waits, [0.01, 0.02])
            self.assertEqual(len(ScriptedHandler.requests), 3)

    def test_auth_failure_is_not_retried(self) -> None:
        with LocalServer([(401, {})]) as server:
            provider = DeepSeekProvider(self.settings(server.url), {"DEEPSEEK_API_KEY": "test-only-token"}, sleeper=lambda _: self.fail("should not retry"))
            with self.assertRaisesRegex(DeepSeekProviderError, "HTTP 401"):
                provider.complete(self.request(), 2)
            self.assertEqual(len(ScriptedHandler.requests), 1)

    def test_startup_rejects_other_environment_and_unsupported_profile(self) -> None:
        provider = DeepSeekProvider(self.settings("https://api.deepseek.com"), {"DEEPSEEK_API_KEY": "test-only-token"})
        with self.assertRaisesRegex(DeepSeekProviderError, "self_hosted_agent"):
            provider.validate_startup(ExecutionProfile("codex_cli", None, "L2", "x", "high", 600, 0))
        with self.assertRaisesRegex(DeepSeekProviderError, "execution profile"):
            provider.validate_startup(ExecutionProfile("self_hosted_agent", "deepseek", "L2", "wrong", "high", 600, 0))

    def test_repository_profiles_cover_flash_pro_and_thinking_modes(self) -> None:
        settings = DeepSeekSettings.from_config(load_initialization_config())
        self.assertEqual(settings.capability_profiles["L1"], ("deepseek-v4-flash", "low"))
        self.assertEqual(settings.capability_profiles["L2"], ("deepseek-v4-flash", "high"))
        self.assertEqual(settings.capability_profiles["L3"], ("deepseek-v4-pro", "low"))
        self.assertEqual(settings.capability_profiles["L5"], ("deepseek-v4-pro", "xhigh"))

    def test_pro_non_thinking_profile_is_sent_to_api(self) -> None:
        with LocalServer([final_response()]) as server:
            provider = DeepSeekProvider(
                self.settings(server.url), {"DEEPSEEK_API_KEY": "test-only-token"}
            )
            response = provider.complete(self.request("L3"), 2)
        self.assertEqual(response["type"], "final")
        self.assertEqual(ScriptedHandler.requests[0]["body"]["model"], "deepseek-v4-pro")
        self.assertEqual(ScriptedHandler.requests[0]["body"]["thinking"], {"type": "disabled"})


if __name__ == "__main__":
    unittest.main()
