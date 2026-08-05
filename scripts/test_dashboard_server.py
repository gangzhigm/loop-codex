from __future__ import annotations

import copy
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

sys.dont_write_bytecode = True

from dashboard_server import (
    DashboardActionError,
    DashboardServer,
    archive_dashboard_task,
    provider_secret_status,
    resolve_attachment_image,
)
from health_run import process_alive
from loopdb import connect, initialize_schema, insert_task, load_initialization_config, now_shanghai, state_payload
from secret_store import SecretStore, SecretStoreCapabilities


class MemorySecretBackend:
    def __init__(self) -> None:
        self.capabilities = SecretStoreCapabilities("memory", "test_memory", True, True, True)
        self.values: dict[tuple[str, str], str] = {}

    def check_available(self) -> None:
        return None

    def read(self, service: str, secret_ref: str) -> str | None:
        return self.values.get((service, secret_ref))

    def write(self, service: str, secret_ref: str, value: str) -> None:
        self.values[(service, secret_ref)] = value

    def delete(self, service: str, secret_ref: str) -> None:
        self.values.pop((service, secret_ref), None)


class AttachmentImageTests(unittest.TestCase):
    def test_dashboard_server_disallows_shared_listener(self) -> None:
        self.assertFalse(DashboardServer.allow_reuse_address)

    def test_current_process_is_detected_as_alive(self) -> None:
        self.assertTrue(process_alive(os.getpid()))
        self.assertFalse(process_alive(2_147_483_647))

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temporary.name)
        self.db_path = self.base_dir / "test.sqlite3"
        self.database = connect(self.db_path)
        initialize_schema(self.database)

    def tearDown(self) -> None:
        self.database.close()
        self.temporary.cleanup()

    def add_task(self, task_id: str, attachment_path: str) -> None:
        insert_task(
            self.database,
            {
                "id": task_id,
                "title": task_id,
                "description": "attachment test",
                "status": "PENDING",
                "priority": "medium",
                "runtime_environment": "codex_automation",
                "created_at": now_shanghai(),
                "scope": ["project/file.txt"],
                "acceptance": ["test"],
                "attachments": [{"path": attachment_path, "role": "source"}],
            },
            actor="test",
            project_paths=["project"],
        )

    def test_registered_image_inside_task_directory_is_resolved(self) -> None:
        attachment_path = "assets/IMAGE-TASK/reference.png"
        self.add_task("IMAGE-TASK", attachment_path)
        image_path = self.base_dir / attachment_path
        image_path.parent.mkdir(parents=True)
        image_path.write_bytes(b"png")

        resolved, content_type = resolve_attachment_image(
            self.database,
            "IMAGE-TASK",
            attachment_path,
            self.base_dir,
        )

        self.assertEqual(resolved, image_path.resolve())
        self.assertEqual(content_type, "image/png")

    def test_dashboard_state_exposes_l1_l5_routes_and_capacity_config(self) -> None:
        self.add_task("DASHBOARD-CODEX", "assets/DASHBOARD-CODEX/reference.png")
        insert_task(
            self.database,
            {
                "id": "DASHBOARD-DEEPSEEK",
                "title": "DASHBOARD-DEEPSEEK",
                "description": "provider route",
                "status": "PENDING",
                "priority": "medium",
                "capability_level": "L5",
                "runtime_environment": "self_hosted_agent",
                "provider_id": "deepseek",
                "execution_policy": "automatic",
                "created_at": now_shanghai(),
                "scope": ["project/deepseek.txt"],
                "acceptance": ["test"],
            },
            actor="test",
            project_paths=["project"],
        )

        payload = state_payload(self.database, load_initialization_config())
        routes = {
            task["id"]: (task["capability_level"], task["runtime_environment"], task["provider_id"])
            for task in payload["tasks"]
        }

        self.assertEqual(payload["schema_version"], "3.4.0")
        self.assertEqual(
            payload["settings"]["platform_max_active_executions"],
            {"codex_automation": 5, "codex_cli": 5, "self_hosted_agent": 5},
        )
        self.assertEqual(payload["settings"]["global_max_active_executions"], 8)
        self.assertEqual(routes["DASHBOARD-CODEX"], ("L2", "codex_automation", None))
        self.assertEqual(routes["DASHBOARD-DEEPSEEK"], ("L5", "self_hosted_agent", "deepseek"))
        self.assertEqual(
            payload["tasks"][0]["execution_policy"],
            "automatic",
        )

    def test_unregistered_path_is_rejected(self) -> None:
        self.add_task("IMAGE-TASK", "assets/IMAGE-TASK/reference.png")
        with self.assertRaises(FileNotFoundError):
            resolve_attachment_image(
                self.database,
                "IMAGE-TASK",
                "assets/IMAGE-TASK/other.png",
                self.base_dir,
            )

    def test_registered_path_outside_task_directory_is_rejected(self) -> None:
        attachment_path = "assets/OTHER-TASK/reference.png"
        self.add_task("IMAGE-TASK", attachment_path)
        with self.assertRaises(PermissionError):
            resolve_attachment_image(
                self.database,
                "IMAGE-TASK",
                attachment_path,
                self.base_dir,
            )

    def test_registered_non_image_is_rejected(self) -> None:
        attachment_path = "assets/IMAGE-TASK/notes.txt"
        self.add_task("IMAGE-TASK", attachment_path)
        file_path = self.base_dir / attachment_path
        file_path.parent.mkdir(parents=True)
        file_path.write_text("notes", encoding="utf-8")
        with self.assertRaises(ValueError):
            resolve_attachment_image(
                self.database,
                "IMAGE-TASK",
                attachment_path,
                self.base_dir,
            )

    def set_status(self, task_id: str, status: str) -> int:
        self.database.execute(
            "UPDATE tasks SET status=?, row_version=row_version+1 WHERE id=?",
            (status, task_id),
        )
        return self.database.execute("SELECT row_version FROM tasks WHERE id=?", (task_id,)).fetchone()[0]

    def test_dashboard_archive_confirms_succeeded_before_archiving(self) -> None:
        self.add_task("ARCHIVE-SUCCEEDED", "assets/ARCHIVE-SUCCEEDED/reference.png")
        row_version = self.set_status("ARCHIVE-SUCCEEDED", "SUCCEEDED")

        result = archive_dashboard_task(self.db_path, "ARCHIVE-SUCCEEDED", "archive", row_version)

        self.assertTrue(result["ok"])
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["outcome"], "ARCHIVED")
        task = self.database.execute(
            "SELECT status, archived_at, row_version FROM tasks WHERE id='ARCHIVE-SUCCEEDED'"
        ).fetchone()
        history = self.database.execute(
            "SELECT from_status, to_status, actor FROM task_history "
            "WHERE task_id='ARCHIVE-SUCCEEDED' ORDER BY id DESC LIMIT 2"
        ).fetchall()
        self.assertEqual(task["status"], "CONFIRMED")
        self.assertIsNotNone(task["archived_at"])
        self.assertEqual(task["row_version"], row_version + 2)
        self.assertEqual(
            [(row["from_status"], row["to_status"], row["actor"]) for row in reversed(history)],
            [
                ("SUCCEEDED", "CONFIRMED", "human-review"),
                ("CONFIRMED", "CONFIRMED", "task-manager"),
            ],
        )

    def test_dashboard_archive_preserves_existing_terminal_status(self) -> None:
        self.add_task("ARCHIVE-FAILED", "assets/ARCHIVE-FAILED/reference.png")
        row_version = self.set_status("ARCHIVE-FAILED", "FAILED")
        self.database.execute(
            "UPDATE tasks SET result_summary='keep summary', result_error='keep error' WHERE id='ARCHIVE-FAILED'"
        )

        result = archive_dashboard_task(self.db_path, "ARCHIVE-FAILED", "archive", row_version)

        self.assertFalse(result["confirmed"])
        task = self.database.execute(
            "SELECT status, archived_at, result_summary, result_error FROM tasks WHERE id='ARCHIVE-FAILED'"
        ).fetchone()
        self.assertEqual(task["status"], "FAILED")
        self.assertIsNotNone(task["archived_at"])
        self.assertEqual((task["result_summary"], task["result_error"]), ("keep summary", "keep error"))

    def test_dashboard_archive_rejects_illegal_status_repeat_and_stale_version(self) -> None:
        self.add_task("ARCHIVE-PENDING", "assets/ARCHIVE-PENDING/reference.png")
        pending_version = self.database.execute(
            "SELECT row_version FROM tasks WHERE id='ARCHIVE-PENDING'"
        ).fetchone()[0]
        with self.assertRaisesRegex(DashboardActionError, "不允许归档"):
            archive_dashboard_task(self.db_path, "ARCHIVE-PENDING", "archive", pending_version)

        self.add_task("ARCHIVE-REPEAT", "assets/ARCHIVE-REPEAT/reference.png")
        archived_version = self.set_status("ARCHIVE-REPEAT", "CANCELLED")
        archive_dashboard_task(self.db_path, "ARCHIVE-REPEAT", "archive", archived_version)
        with self.assertRaisesRegex(DashboardActionError, "状态已变化"):
            archive_dashboard_task(self.db_path, "ARCHIVE-REPEAT", "archive", archived_version)

        self.add_task("ARCHIVE-STALE", "assets/ARCHIVE-STALE/reference.png")
        stale_version = self.set_status("ARCHIVE-STALE", "CONFIRMED")
        self.set_status("ARCHIVE-STALE", "FAILED")
        with self.assertRaisesRegex(DashboardActionError, "状态已变化"):
            archive_dashboard_task(self.db_path, "ARCHIVE-STALE", "archive", stale_version)
        self.assertIsNone(
            self.database.execute("SELECT archived_at FROM tasks WHERE id='ARCHIVE-STALE'").fetchone()[0]
        )

    def test_dashboard_archive_validates_action_input(self) -> None:
        for task_id, action, row_version in [
            ("bad id", "archive", 1),
            ("VALID-ID", "delete", 1),
            ("VALID-ID", "archive", True),
        ]:
            with self.subTest(task_id=task_id, action=action, row_version=row_version):
                with self.assertRaises(DashboardActionError):
                    archive_dashboard_task(self.db_path, task_id, action, row_version)


class SecretApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temporary.name)
        self.health_state = self.base_dir / "health-state.json"
        self.health_state.write_text(
            json.dumps({"component": "dashboard-server", "status": "HEALTHY", "events": []}),
            encoding="utf-8",
        )
        self.backend = MemorySecretBackend()
        self.store = SecretStore(self.backend, "Loop Dashboard Tests", current_account="test-account")
        self.config: dict[str, Any] = copy.deepcopy(load_initialization_config())
        self.config["dashboard"]["secret_api"]["max_body_bytes"] = 2048
        self.config["dashboard"]["secret_api"]["replay_cache_size"] = 32
        self.server = DashboardServer(
            ("127.0.0.1", 0),
            self.base_dir / "unused.sqlite3",
            Path(__file__).resolve().parents[1] / "dashboard.html",
            self.config,
            secret_store=self.store,
            health_state_path=self.health_state,
            provider_verifiers={"deepseek": lambda candidate: not candidate.startswith("reject-")},
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str = "/api/secrets",
        *,
        payload: dict[str, object] | None = None,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        secure_headers: bool = True,
    ) -> tuple[int, dict[str, str], dict[str, Any]]:
        request_headers = {"Host": self.server.expected_host}
        if secure_headers and method == "POST":
            request_headers.update(
                {
                    "Origin": self.server.expected_origin,
                    "X-CSRF-Token": self.server.csrf_token,
                    "Content-Type": "application/json",
                }
            )
        if headers:
            request_headers.update(headers)
        request_body = body
        if payload is not None:
            request_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            connection.request(method, path, body=request_body, headers=request_headers)
            response = connection.getresponse()
            response_body = response.read().decode("utf-8")
            return response.status, dict(response.getheaders()), json.loads(response_body)
        finally:
            connection.close()

    @staticmethod
    def mutation(action: str, **values: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "provider_id": "deepseek",
            "action": action,
            "request_id": str(uuid4()),
        }
        payload.update(values)
        return payload

    def csrf_token(self) -> str:
        status, headers, _payload = self.request("GET")
        self.assertEqual(status, 200)
        return headers["X-CSRF-Token"]

    def test_provider_secret_lifecycle_returns_only_non_sensitive_metadata(self) -> None:
        status, headers, payload = self.request("GET")
        self.assertEqual(status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(headers["X-CSRF-Token"], self.server.csrf_token)
        provider = payload["providers"][0]
        self.assertEqual(
            set(provider),
            {
                "provider_id",
                "configured",
                "backend",
                "status",
                "last_validated_at",
                "validation_scope",
                "persistent",
                "mutable",
                "repair",
            },
        )
        self.assertEqual((provider["provider_id"], provider["configured"], provider["status"]), ("deepseek", False, "not_configured"))

        original = "dashboard-test-secret-original"
        status, _headers, payload = self.request(
            "POST",
            payload=self.mutation(
                "set", secret=original, connect=False, confirmation="SET"
            ),
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["provider"]["status"], "configured_unverified")
        self.assertNotIn(original, json.dumps(payload, ensure_ascii=False))
        self.assertNotIn(original[-4:], json.dumps(payload, ensure_ascii=False))

        status, _headers, payload = self.request(
            "POST",
            payload=self.mutation("verify", connect=False, confirmation="VERIFY"),
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["provider"]["status"], "valid")
        self.assertEqual(payload["provider"]["validation_scope"], "local")
        self.assertRegex(payload["provider"]["last_validated_at"], r"\+08:00$")

        replacement = "dashboard-test-secret-replacement"
        status, _headers, payload = self.request(
            "POST",
            payload=self.mutation(
                "rotate",
                secret=replacement,
                connect=True,
                confirmation="ROTATE_CONNECT",
            ),
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["provider"]["validation_scope"], "connection")
        self.assertNotIn(replacement, json.dumps(payload, ensure_ascii=False))

        status, _headers, payload = self.request(
            "POST", payload=self.mutation("delete", confirmation="DELETE")
        )
        self.assertEqual(status, 200)
        self.assertEqual((payload["provider"]["configured"], payload["provider"]["status"]), (False, "not_configured"))
        health = self.health_state.read_text(encoding="utf-8")
        self.assertNotIn(original, health)
        self.assertNotIn(replacement, health)
        self.assertIn('"provider_id": "deepseek"', health)

    def test_invalid_connection_status_is_recorded_without_value_disclosure(self) -> None:
        candidate = "reject-dashboard-test-secret"
        status, _headers, _payload = self.request(
            "POST",
            payload=self.mutation(
                "set", secret=candidate, connect=False, confirmation="SET"
            ),
        )
        self.assertEqual(status, 200)
        status, _headers, payload = self.request(
            "POST",
            payload=self.mutation("verify", connect=True, confirmation="CONNECT"),
        )
        self.assertEqual(status, 422)
        self.assertNotIn(candidate, json.dumps(payload, ensure_ascii=False))
        status, _headers, payload = self.request("GET")
        self.assertEqual(status, 200)
        self.assertEqual(payload["providers"][0]["status"], "invalid")
        self.assertEqual(payload["providers"][0]["validation_scope"], "connection")

    def test_secret_api_rejects_dns_rebinding_cross_site_forms_and_bad_csrf(self) -> None:
        status, headers, _payload = self.request("GET", headers={"Host": "attacker.example"})
        self.assertEqual(status, 421)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

        status, headers, _payload = self.request("OPTIONS", secure_headers=False)
        self.assertEqual(status, 403)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

        sample = self.mutation(
            "set", secret="security-test-secret-value", connect=False, confirmation="SET"
        )
        status, _headers, _payload = self.request(
            "POST", payload=sample, headers={"Origin": "http://attacker.example"}
        )
        self.assertEqual(status, 403)
        status, _headers, _payload = self.request(
            "POST", payload=sample, headers={"X-CSRF-Token": "invalid-csrf-token"}
        )
        self.assertEqual(status, 403)
        status, _headers, _payload = self.request(
            "POST",
            body=b"provider_id=deepseek",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 415)
        status, _headers, _payload = self.request(
            "POST", body=b"{}" + b"x" * 4096
        )
        self.assertEqual(status, 413)

    def test_invalid_provider_and_replayed_request_are_rejected(self) -> None:
        invalid = self.mutation(
            "set", secret="provider-test-secret-value", connect=False, confirmation="SET"
        )
        invalid["provider_id"] = "unknown"
        status, _headers, _payload = self.request("POST", payload=invalid)
        self.assertEqual(status, 400)

        replay = self.mutation(
            "set", secret="replay-test-secret-value", connect=False, confirmation="SET"
        )
        status, _headers, _payload = self.request("POST", payload=replay)
        self.assertEqual(status, 200)
        status, _headers, payload = self.request("POST", payload=replay)
        self.assertEqual(status, 409)
        self.assertIn("重复请求", payload["error"])

    def test_failure_request_logging_redacts_query_and_submitted_secret(self) -> None:
        submitted = "sk-dashboard-logging-test-secret"
        request = self.mutation(
            "set", secret=submitted, connect=False, confirmation="SET"
        )
        with patch("builtins.print") as printer:
            status, _headers, payload = self.request(
                "POST", "/api/secrets?api_key=" + submitted, payload=request
            )
        self.assertEqual(status, 400)
        serialized = repr(printer.call_args_list) + json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(submitted, serialized)
        self.assertNotIn("api_key=", serialized)

    def test_account_mismatch_is_unavailable_with_non_copy_repair_guidance(self) -> None:
        mismatch = SecretStore(
            self.backend,
            "Loop Dashboard Tests",
            expected_account="configured-account",
            current_account="other-account",
        )
        status = provider_secret_status(
            mismatch, "deepseek", "DEEPSEEK_API_KEY", self.health_state
        )
        self.assertIsNone(status["configured"])
        self.assertEqual(status["status"], "storage_unavailable")
        self.assertIn("不要把密钥静默复制", status["repair"])


if __name__ == "__main__":
    unittest.main()
