from __future__ import annotations

# 中文排查：覆盖 Dashboard 路由、同源安全、任务动作、运维配置、Secret API 和静态资源。
# HTTP 状态不符时先看请求前置条件，再看 Handler 分支和被 mock 的控制面返回。
# 测试使用内存 Secret 后端和临时服务，不应读取或依赖本机真实密钥。

import argparse
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

from _bootstrap import REPOSITORY_ROOT

from roles.supervisor.dashboard_server import (
    DashboardActionError,
    DashboardServer,
    archive_dashboard_task,
    operations_config_payload,
    provider_secret_status,
    recover_dashboard_task,
    resolve_attachment_image,
)
from roles.supervisor.health_run import process_alive
from loopdb import DEFAULT_DB, connect, initialize_schema, insert_task, load_initialization_config, now_shanghai, state_payload
from loop_agent.secrets.store import SecretStore, SecretStoreCapabilities


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

    def add_task(
        self,
        task_id: str,
        attachment_path: str,
        *,
        priority: str = "medium",
        lock_mode: str = "project",
        scope: list[str] | None = None,
    ) -> None:
        insert_task(
            self.database,
            {
                "id": task_id,
                "title": task_id,
                "description": "attachment test",
                "status": "PENDING",
                "priority": priority,
                "runtime_environment": "codex_automation",
                "created_at": now_shanghai(),
                "scope": scope or ["project/file.txt"],
                "lock_mode": lock_mode,
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

        self.assertEqual(payload["schema_version"], "3.7.0")
        self.assertEqual(
            payload["settings"]["platform_max_active_executions"],
            {"codex_automation": 5, "codex_cli": 5, "self_hosted_agent": 5},
        )
        self.assertEqual(payload["settings"]["global_max_active_executions"], 8)
        self.assertEqual(routes["DASHBOARD-CODEX"], ("L2", "codex_automation", None))
        self.assertEqual(routes["DASHBOARD-DEEPSEEK"], ("L5", "self_hosted_agent", "deepseek"))
        self.assertEqual(payload["planners"], [])
        self.assertEqual(payload["planner_settings"]["execution_kind"], "PLANNER")
        self.assertEqual(payload["tasks"][0]["preflight_status"], "READY")
        self.assertIn("operator_definition", payload["tasks"][0])
        self.assertIn("planner_supplement", payload["tasks"][0])
        self.assertEqual(
            payload["tasks"][0]["execution_policy"],
            "automatic",
        )

    def test_dashboard_state_projects_dynamic_scope_blockers_and_queue_positions(self) -> None:
        self.add_task(
            "ACTIVE-FILE", "assets/ACTIVE-FILE/reference.png", priority="critical",
            lock_mode="file", scope=["project/src/shared.py"],
        )
        self.add_task(
            "PENDING-HIGH", "assets/PENDING-HIGH/reference.png", priority="high",
            lock_mode="file", scope=["PROJECT\\src\\.\\SHARED.py"],
        )
        self.add_task(
            "PENDING-LOW", "assets/PENDING-LOW/reference.png", priority="low",
            lock_mode="file", scope=["project/src/shared.py"],
        )
        stamp = now_shanghai()
        self.database.execute(
            "UPDATE tasks SET status='RUNNING', assigned_agent='active-file-exec', started_at=?, "
            "heartbeat_at=?, attempt=1 WHERE id='ACTIVE-FILE'",
            (stamp, stamp),
        )
        self.database.execute(
            "INSERT INTO executions(execution_id, task_id, status, started_at, heartbeat_at, "
            "lease_expires_at, runtime_environment, provider_id, capability_level, execution_policy, "
            "model, reasoning, attempt_timeout_seconds, max_retries) VALUES("
            "'active-file-exec', 'ACTIVE-FILE', 'RUNNING', ?, ?, '2999-01-01T00:00:00+08:00', "
            "'codex_automation', NULL, 'L2', 'automatic', 'gpt-5.6-terra', 'medium', 3600, 0)",
            (stamp, stamp),
        )
        self.database.execute(
            "INSERT INTO scope_locks(scope_key, task_id, execution_id, acquired_at, lease_expires_at) "
            "VALUES('file:project::src/shared.py', 'ACTIVE-FILE', 'active-file-exec', ?, "
            "'2999-01-01T00:00:00+08:00')",
            (stamp,),
        )

        payload = state_payload(self.database, load_initialization_config())
        tasks = {task["id"]: task for task in payload["tasks"]}

        self.assertEqual(tasks["PENDING-HIGH"]["status"], "PENDING")
        self.assertEqual(tasks["PENDING-HIGH"]["blocked_by_task_ids"], ["ACTIVE-FILE"])
        self.assertEqual(tasks["PENDING-HIGH"]["blocked_scopes"], ["project/src/SHARED.py"])
        self.assertEqual(
            tasks["PENDING-HIGH"]["blocked_scope_keys"], ["file:project::src/shared.py"]
        )
        self.assertEqual(tasks["PENDING-HIGH"]["scope_queue_position"], 1)
        self.assertEqual(tasks["PENDING-LOW"]["scope_queue_position"], 2)
        self.assertEqual(
            tasks["PENDING-HIGH"]["blocking_scopes"][0]["blocker_lock_status"], "ACTIVE"
        )

    def test_served_dashboard_accepts_schema_3_7(self) -> None:
        html = (REPOSITORY_ROOT / "frontend" / "dashboard.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('const TASK_SCHEMA_VERSION = "3.7.0";', html)
        self.assertNotIn('const TASK_SCHEMA_VERSION = "3.6.0";', html)

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

    def make_recovery(self, task_id: str = "RECOVERY-TASK") -> tuple[str, int]:
        self.add_task(task_id, f"assets/{task_id}/reference.png")
        execution_id = f"L5-worker-{task_id.lower()}"
        stamp = now_shanghai()
        self.database.execute(
            "UPDATE tasks SET status='WAITING_HUMAN', capability_level='L5', assigned_agent=?, "
            "started_at=?, heartbeat_at=?, human_required=1, human_question='确认旧会话', "
            "human_options_json='[]', human_requested_at=?, row_version=row_version+1 WHERE id=?",
            (execution_id, stamp, stamp, stamp, task_id),
        )
        self.database.execute(
            """INSERT INTO executions(
              execution_id, task_id, status, started_at, heartbeat_at, lease_expires_at, finished_at,
              outcome, runtime_environment, provider_id, capability_level, execution_policy, model,
              reasoning, attempt_timeout_seconds, max_retries, termination_reason, recovery_required
            ) VALUES(?, ?, 'STALLED', ?, ?, ?, ?, 'RECOVERY_REQUIRED', 'codex_automation', NULL,
              'L5', 'automatic', 'gpt-5.6-sol', 'xhigh', 14400, 0, 'HEARTBEAT_STALLED', 1)""",
            (execution_id, task_id, stamp, stamp, stamp, stamp),
        )
        scope = self.database.execute(
            "SELECT scope_key FROM task_scopes WHERE task_id=?", (task_id,)
        ).fetchone()[0]
        self.database.execute(
            """INSERT INTO scope_locks(
              scope_key, task_id, execution_id, acquired_at, lease_expires_at, status,
              quarantined_at, quarantine_reason
            ) VALUES(?, ?, ?, ?, ?, 'QUARANTINED', ?, '心跳停滞')""",
            (scope, task_id, execution_id, stamp, stamp, stamp),
        )
        row_version = self.database.execute(
            "SELECT row_version FROM tasks WHERE id=?", (task_id,)
        ).fetchone()[0]
        return execution_id, row_version

    def test_dashboard_state_exposes_inactive_execution_and_quarantined_scope(self) -> None:
        execution_id, _ = self.make_recovery()

        payload = state_payload(self.database, load_initialization_config())

        self.assertEqual(payload["agents"], [])
        self.assertEqual(payload["recoveries"][0]["execution_id"], execution_id)
        self.assertEqual(payload["recoveries"][0]["execution_status"], "STALLED")
        self.assertEqual(payload["recoveries"][0]["scope_status"], "QUARANTINED")

    def test_dashboard_recovery_requires_confirmation_and_requeues_transactionally(self) -> None:
        execution_id, row_version = self.make_recovery()
        with self.assertRaisesRegex(DashboardActionError, "明确确认"):
            recover_dashboard_task(
                self.db_path, "RECOVERY-TASK", execution_id, "requeue", row_version, False
            )

        result = recover_dashboard_task(
            self.db_path, "RECOVERY-TASK", execution_id, "requeue", row_version, True
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["task_status"], "PENDING")
        task = self.database.execute(
            "SELECT status, assigned_agent FROM tasks WHERE id='RECOVERY-TASK'"
        ).fetchone()
        self.assertEqual((task["status"], task["assigned_agent"]), ("PENDING", None))
        self.assertEqual(
            self.database.execute(
                "SELECT count(*) FROM scope_locks WHERE execution_id=?", (execution_id,)
            ).fetchone()[0],
            0,
        )

    def test_dashboard_continue_waiting_is_idempotent_and_keeps_quarantine(self) -> None:
        execution_id, row_version = self.make_recovery()
        first = recover_dashboard_task(
            self.db_path, "RECOVERY-TASK", execution_id, "wait", row_version, True
        )
        next_version = self.database.execute(
            "SELECT row_version FROM tasks WHERE id='RECOVERY-TASK'"
        ).fetchone()[0]
        second = recover_dashboard_task(
            self.db_path, "RECOVERY-TASK", execution_id, "wait", next_version, True
        )

        self.assertEqual(first["outcome"], "WAITING")
        self.assertEqual(second["outcome"], "ALREADY_WAITING")
        self.assertEqual(
            self.database.execute(
                "SELECT status FROM scope_locks WHERE execution_id=?", (execution_id,)
            ).fetchone()[0],
            "QUARANTINED",
        )

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
        self.workspace = self.base_dir / "workspace"
        self.next_workspace = self.base_dir / "next-workspace"
        for root in (self.workspace, self.next_workspace):
            (root / "project").mkdir(parents=True)
            (root / "根目录清单.md").write_text(
                "| 文件夹名 | 说明 |\n| --- | --- |\n| `project` | test project |\n",
                encoding="utf-8",
            )
        self.config["workspace"]["task_root"] = str(self.workspace)
        self.config["workspace"]["project_registry"] = str(self.workspace / "根目录清单.md")
        self.config["dashboard"]["secret_api"]["max_body_bytes"] = 2048
        self.config["dashboard"]["secret_api"]["replay_cache_size"] = 32
        self.config_path = self.base_dir / "initialization.json"
        self.config_path.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")
        self.db_path = self.base_dir / "unused.sqlite3"
        database = connect(self.db_path)
        initialize_schema(database)
        database.close()
        self.server = DashboardServer(
            ("127.0.0.1", 0),
            self.db_path,
            REPOSITORY_ROOT / "frontend" / "dashboard.html",
            self.config,
            runtime_config_path=self.config_path,
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

    def raw_request(self, path: str) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            connection.request("GET", path, headers={"Host": self.server.expected_host})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_operations_page_and_allowlisted_configuration_are_available(self) -> None:
        for path, content_type in [
            ("/operations.html", "text/html"),
            ("/operations.js", "application/javascript"),
            ("/operations.css", "text/css"),
        ]:
            status, headers, body = self.raw_request(path)
            self.assertEqual(status, 200)
            self.assertIn(content_type, headers["Content-Type"])
            self.assertTrue(body)

        status, _headers, payload = self.request("GET", "/api/operations-config")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            [section["id"] for section in payload["sections"]],
            ["system", "ai-configuration", "operator", "planner", "supervisor", "dispatcher", "worker", "runner"],
        )
        serialized = json.dumps(payload, ensure_ascii=False).casefold()
        for sensitive_name in ["secret_ref", "deepseek_api_key", "authorization", "hidden_reasoning", "request_body", "response_body"]:
            self.assertNotIn(sensitive_name, serialized)
        secret_item = next(item for section in payload["sections"] if section["id"] == "ai-configuration" for item in section["items"] if item["key"] == "provider-status")
        provider = secret_item["value"][0]
        self.assertEqual(set(provider), {"provider_id", "configured", "backend", "status", "last_validated_at", "validation_scope", "persistent", "mutable", "repair"})

    def test_operations_projection_never_forwards_runtime_configuration(self) -> None:
        payload = operations_config_payload(
            self.config,
            self.store,
            self.server.provider_secret_refs,
            self.health_state,
        )
        self.assertNotIn("runtime_config", payload)
        self.assertNotIn("secret_ref", json.dumps(payload, ensure_ascii=False).casefold())

    def test_task_root_editor_selects_and_atomically_updates_a_complete_workspace(self) -> None:
        selection = {"action": "select_task_root", "request_id": str(uuid4())}
        with patch(
            "roles.supervisor.dashboard_server.choose_task_root",
            return_value=self.next_workspace,
        ):
            status, _headers, payload = self.request("POST", "/api/operations-config/action", payload=selection)
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "outcome": "SELECTED", "task_root": str(self.next_workspace)})

        update = {
            "action": "set_task_root",
            "request_id": str(uuid4()),
            "task_root": str(self.next_workspace),
            "confirmation": "SET_TASK_ROOT",
        }
        status, _headers, payload = self.request("POST", "/api/operations-config/action", payload=update)
        self.assertEqual(status, 200)
        self.assertEqual(payload["outcome"], "UPDATED")
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["workspace"]["task_root"], str(self.next_workspace))
        self.assertEqual(saved["workspace"]["project_registry"], str(self.next_workspace / "根目录清单.md"))
        self.assertEqual(self.server.runtime_config["workspace"]["task_root"], str(self.next_workspace))

    def test_task_root_editor_rejects_active_execution_and_cross_site_requests(self) -> None:
        update = {
            "action": "set_task_root",
            "request_id": str(uuid4()),
            "task_root": str(self.next_workspace),
            "confirmation": "SET_TASK_ROOT",
        }
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        database = connect(self.db_path)
        try:
            insert_task(
                database,
                {
                    "id": "ACTIVE-TASK",
                    "title": "active",
                    "description": "active",
                    "status": "PENDING",
                    "priority": "medium",
                    "runtime_environment": "codex_automation",
                    "created_at": now_shanghai(),
                    "scope": ["project/file.txt"],
                    "acceptance": ["test"],
                },
                actor="test",
                project_paths=["project"],
            )
            database.execute(
                "UPDATE tasks SET status='RUNNING' WHERE id='ACTIVE-TASK'",
            )
            database.execute(
                """INSERT INTO executions(
                  execution_id, task_id, status, started_at, heartbeat_at, lease_expires_at,
                  runtime_environment, capability_level, execution_policy, model, reasoning,
                  attempt_timeout_seconds, max_retries
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "active-execution", "ACTIVE-TASK", "RUNNING", now_shanghai(), now_shanghai(), now_shanghai(),
                    "codex_automation", "L1", "automatic", "test-model", "low", 60, 0,
                ),
            )
            database.commit()
        finally:
            database.close()
        status, _headers, payload = self.request("POST", "/api/operations-config/action", payload=update)
        self.assertEqual(status, 409)
        self.assertIn("活动 execution", payload["error"])

        status, _headers, payload = self.request(
            "POST",
            "/api/operations-config/action",
            payload={"action": "select_task_root", "request_id": str(uuid4())},
            headers={"Origin": "http://example.test"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "Origin 无效")

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
            "POST",
            body=b'{"provider_id":"deepseek"}',
            headers={"Content-Type": "text/plain"},
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

    def test_server_rejects_non_loopback_binding_before_opening_a_listener(self) -> None:
        with self.assertRaisesRegex(ValueError, "127.0.0.1"):
            DashboardServer(
                ("0.0.0.0", 0),
                self.base_dir / "unused.sqlite3",
                REPOSITORY_ROOT / "frontend" / "dashboard.html",
                self.config,
                secret_store=self.store,
                health_state_path=self.health_state,
                provider_verifiers={},
            )


def run_visual_server(port: int) -> None:
    temporary = tempfile.TemporaryDirectory()
    config: dict[str, Any] = copy.deepcopy(load_initialization_config())
    store = SecretStore(MemorySecretBackend(), "Dashboard Visual Tests", current_account="test-account")
    server = DashboardServer(
        ("127.0.0.1", port),
        DEFAULT_DB,
        REPOSITORY_ROOT / "frontend" / "dashboard.html",
        config,
        secret_store=store,
        health_state_path=Path(temporary.name) / "health-state.json",
        provider_verifiers={"deepseek": lambda _candidate: True},
    )
    print(f"visual test server listening on http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        temporary.cleanup()


if __name__ == "__main__":
    if "--visual-server" in sys.argv:
        visual_parser = argparse.ArgumentParser()
        visual_parser.add_argument("--visual-server", action="store_true")
        visual_parser.add_argument("--port", type=int, default=4181)
        visual_args = visual_parser.parse_args()
        run_visual_server(visual_args.port)
    else:
        unittest.main()
