from __future__ import annotations

# 中文排查：Dashboard Server 提供只读状态、受控任务动作、运维配置和本机 Secret API。
# HTTP 异常先看路由和请求安全校验，再看 loop_agent/dashboard；任务写入只能调用 loopctl。
# 监听地址必须保持 127.0.0.1，不能通过修改本文件临时开放远程密钥管理。

import argparse
import copy
import hmac
import json
import os
import secrets
import signal
import sys
import threading
import uuid
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlparse

sys.dont_write_bytecode = True

SCRIPTS_ROOT = Path(__file__).resolve().parents[2]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from loopdb import (
    BASE_DIR,
    CONFIG_PATH,
    DEFAULT_DB,
    SCHEMA_VERSION,
    connect,
    load_initialization_config,
    now_shanghai,
    parse_project_registry,
    schema_version,
    state_payload,
)
from loop_agent.secrets.store import (
    SecretAccessDenied,
    SecretNotFound,
    SecretOperationUnsupported,
    SecretStore,
    SecretStoreError,
    SecretStoreUnavailable,
    SecretValidationError,
    create_secret_store,
)


TASK_ACTION_PATH = "/api/task-action"
SECRET_API_PATH = "/api/secrets"
OPERATIONS_API_PATH = "/api/operations-config"
OPERATIONS_ACTION_PATH = "/api/operations-config/action"
OPERATIONS_ASSETS = {
    "/operations.html": ("operations.html", "text/html; charset=utf-8"),
    "/operations.js": ("operations.js", "application/javascript; charset=utf-8"),
    "/operations.css": ("operations.css", "text/css; charset=utf-8"),
}
MAX_ACTION_BODY_BYTES = 4096


class SecretApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


from loop_agent.dashboard.operations import (
    OperationsApiError,
    choose_task_root,
    connection_verifiers,
    operations_config_payload,
    provider_secret_refs,
    provider_secret_status,
    record_provider_secret_event,
    validate_task_root,
    write_task_root_config,
)
from loop_agent.dashboard.tasks import (
    HEALTH_STATE,
    DashboardActionError,
    archive_dashboard_task,
    recover_dashboard_task,
    resolve_attachment_image,
    run_loopctl,
    runtime_health,
)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        address: tuple[str, int],
        database_path: Path,
        dashboard_path: Path,
        runtime_config: dict[str, object],
        *,
        runtime_config_path: Path = CONFIG_PATH,
        secret_store: SecretStore | None = None,
        health_state_path: Path = HEALTH_STATE,
        provider_verifiers: Mapping[str, Callable[[str], bool | None]] | None = None,
    ):
        if address[0] != "127.0.0.1":
            raise ValueError("Dashboard Server with Secret API must bind to 127.0.0.1")
        dashboard_config = runtime_config.get("dashboard")
        secret_api_config = dashboard_config.get("secret_api") if isinstance(dashboard_config, Mapping) else None
        if not isinstance(secret_api_config, Mapping) or secret_api_config.get("enabled") is not True:
            raise ValueError("dashboard.secret_api must be enabled")
        max_body_bytes = secret_api_config.get("max_body_bytes")
        replay_cache_size = secret_api_config.get("replay_cache_size")
        if not isinstance(max_body_bytes, int) or not 1024 <= max_body_bytes <= 65536:
            raise ValueError("dashboard.secret_api.max_body_bytes is invalid")
        if not isinstance(replay_cache_size, int) or not 16 <= replay_cache_size <= 8192:
            raise ValueError("dashboard.secret_api.replay_cache_size is invalid")
        super().__init__(address, DashboardHandler)
        self.database_path = database_path
        self.dashboard_path = dashboard_path
        self.runtime_config_path = runtime_config_path.resolve()
        self.operations_paths = {
            route: dashboard_path.with_name(filename)
            for route, (filename, _content_type) in OPERATIONS_ASSETS.items()
        }
        self.runtime_config = runtime_config
        self.secret_store = secret_store or create_secret_store(runtime_config)
        self.provider_secret_refs = provider_secret_refs(runtime_config)
        self.provider_verifiers = dict(
            connection_verifiers(runtime_config) if provider_verifiers is None else provider_verifiers
        )
        self.health_state_path = health_state_path
        self.secret_api_max_body_bytes = max_body_bytes
        self._request_ids: deque[str] = deque()
        self._request_id_set: set[str] = set()
        self._request_id_lock = threading.Lock()
        self.csrf_token = secrets.token_urlsafe(32)
        self.expected_host = f"127.0.0.1:{self.server_address[1]}"
        self.expected_origin = f"http://{self.expected_host}"
        self.replay_cache_size = replay_cache_size
        self._operations_config_lock = threading.RLock()

    def reserve_request_id(self, value: object) -> str:
        if not isinstance(value, str):
            raise SecretApiError(HTTPStatus.BAD_REQUEST, "request_id 无效")
        try:
            parsed = uuid.UUID(value)
        except (ValueError, AttributeError):
            raise SecretApiError(HTTPStatus.BAD_REQUEST, "request_id 无效") from None
        if parsed.version != 4 or str(parsed) != value:
            raise SecretApiError(HTTPStatus.BAD_REQUEST, "request_id 无效")
        with self._request_id_lock:
            if value in self._request_id_set:
                raise SecretApiError(HTTPStatus.CONFLICT, "重复请求已拒绝")
            if len(self._request_ids) >= self.replay_cache_size:
                expired = self._request_ids.popleft()
                self._request_id_set.discard(expired)
            self._request_ids.append(value)
            self._request_id_set.add(value)
        return value


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer

    def log_message(self, format_string: str, *args: object) -> None:
        if urlparse(self.path).path == "/api/state" and len(args) > 1 and str(args[1]) == "200":
            return
        if urlparse(self.path).path == SECRET_API_PATH:
            status = str(args[1]) if len(args) > 1 else "-"
            print(
                f'{now_shanghai()} {self.client_address[0]} "{self.command} {SECRET_API_PATH}" {status}',
                flush=True,
            )
            return
        print(f"{now_shanghai()} {self.client_address[0]} {format_string % args}", flush=True)

    def send_bytes(
        self,
        status: int,
        content_type: str,
        body: bytes,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(
        self,
        status: int,
        payload: dict[str, object],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_bytes(
            status,
            "application/json; charset=utf-8",
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
        )

    def _require_secret_host(self) -> None:
        values = self.headers.get_all("Host", failobj=[])
        if len(values) != 1 or not hmac.compare_digest(values[0], self.server.expected_host):
            raise SecretApiError(HTTPStatus.MISDIRECTED_REQUEST, "Host 无效")

    def _require_secret_origin(self) -> None:
        origins = self.headers.get_all("Origin", failobj=[])
        if len(origins) != 1 or not hmac.compare_digest(origins[0], self.server.expected_origin):
            raise SecretApiError(HTTPStatus.FORBIDDEN, "Origin 无效")
        fetch_site = self.headers.get("Sec-Fetch-Site")
        if fetch_site is not None and fetch_site != "same-origin":
            raise SecretApiError(HTTPStatus.FORBIDDEN, "跨站请求已拒绝")

    def _require_csrf_token(self) -> None:
        values = self.headers.get_all("X-CSRF-Token", failobj=[])
        if len(values) != 1 or not hmac.compare_digest(values[0], self.server.csrf_token):
            raise SecretApiError(HTTPStatus.FORBIDDEN, "CSRF token 无效")

    def _read_secret_json(self) -> dict[str, object]:
        if self.headers.get("Transfer-Encoding") is not None:
            raise SecretApiError(HTTPStatus.BAD_REQUEST, "不支持 Transfer-Encoding")
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise SecretApiError(HTTPStatus.BAD_REQUEST, "Content-Length 无效") from None
        if content_length < 1:
            raise SecretApiError(HTTPStatus.BAD_REQUEST, "请求体为空")
        if content_length > self.server.secret_api_max_body_bytes:
            raise SecretApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "请求体超过大小限制")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self.rfile.read(content_length)
            raise SecretApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type 必须为 application/json")
        try:
            body = self.rfile.read(content_length)
            if len(body) != content_length:
                raise ValueError
            payload = json.loads(body.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise SecretApiError(HTTPStatus.BAD_REQUEST, "请求 JSON 无效") from None
        if not isinstance(payload, dict):
            raise SecretApiError(HTTPStatus.BAD_REQUEST, "请求 JSON 必须为对象")
        return payload

    def _secret_status_payload(self) -> dict[str, object]:
        providers = [
            provider_secret_status(
                self.server.secret_store,
                provider_id,
                secret_ref,
                self.server.health_state_path,
            )
            for provider_id, secret_ref in sorted(self.server.provider_secret_refs.items())
        ]
        return {"ok": True, "providers": providers}

    def _operations_config_payload(self) -> dict[str, object]:
        return operations_config_payload(
            self.server.runtime_config,
            self.server.secret_store,
            self.server.provider_secret_refs,
            self.server.health_state_path,
        )

    def _read_operations_json(self) -> dict[str, object]:
        if self.headers.get("Transfer-Encoding") is not None:
            raise OperationsApiError(HTTPStatus.BAD_REQUEST, "不支持 Transfer-Encoding")
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise OperationsApiError(HTTPStatus.BAD_REQUEST, "Content-Length 无效") from None
        if content_length < 1 or content_length > self.server.secret_api_max_body_bytes:
            raise OperationsApiError(HTTPStatus.BAD_REQUEST, "请求体无效")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self.rfile.read(content_length)
            raise OperationsApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type 必须为 application/json")
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise OperationsApiError(HTTPStatus.BAD_REQUEST, "请求 JSON 无效") from None
        if not isinstance(payload, dict):
            raise OperationsApiError(HTTPStatus.BAD_REQUEST, "请求 JSON 必须为对象")
        return payload

    def _active_execution_count(self) -> int:
        database = connect(self.server.database_path)
        try:
            return int(database.execute("SELECT count(*) FROM executions WHERE status='RUNNING'").fetchone()[0])
        finally:
            database.close()

    def _handle_operations_action(self, request: object) -> None:
        if getattr(request, "query", ""):
            raise OperationsApiError(HTTPStatus.BAD_REQUEST, "运维操作接口不接受查询参数")
        self._require_secret_host()
        self._require_secret_origin()
        self._require_csrf_token()
        payload = self._read_operations_json()
        action = payload.get("action")
        if action == "select_task_root":
            if set(payload) != {"action", "request_id"}:
                raise OperationsApiError(HTTPStatus.BAD_REQUEST, "选择工作区请求字段无效")
            self.server.reserve_request_id(payload["request_id"])
            current = Path(str(self.server.runtime_config["workspace"]["task_root"]))
            with self.server._operations_config_lock:
                selected = choose_task_root(current)
            self.send_json(
                HTTPStatus.OK,
                {"ok": True, "outcome": "CANCELLED" if selected is None else "SELECTED", "task_root": str(selected) if selected else None},
            )
            return
        if action != "set_task_root" or set(payload) != {"action", "request_id", "task_root", "confirmation"}:
            raise OperationsApiError(HTTPStatus.BAD_REQUEST, "运维操作无效")
        if payload.get("confirmation") != "SET_TASK_ROOT":
            raise OperationsApiError(HTTPStatus.FORBIDDEN, "修改全局任务工作区未获得明确确认")
        candidate = payload.get("task_root")
        if not isinstance(candidate, str) or not candidate.strip():
            raise OperationsApiError(HTTPStatus.BAD_REQUEST, "全局任务工作区无效")
        self.server.reserve_request_id(payload["request_id"])
        with self.server._operations_config_lock:
            if self._active_execution_count():
                raise OperationsApiError(HTTPStatus.CONFLICT, "存在活动 execution，不能修改全局任务工作区")
            raw_root = Path(candidate)
            if not raw_root.is_absolute():
                raise OperationsApiError(HTTPStatus.BAD_REQUEST, "全局任务工作区必须是绝对路径")
            root, registry = validate_task_root(raw_root, self.server.runtime_config)
            updated = write_task_root_config(
                self.server.runtime_config_path,
                self.server.runtime_config,
                root,
                registry,
            )
            self.server.runtime_config = updated
        self.send_json(HTTPStatus.OK, {"ok": True, "outcome": "UPDATED", "task_root": str(root)})

    def _handle_secret_get(self, request: object) -> None:
        if getattr(request, "query", ""):
            raise SecretApiError(HTTPStatus.BAD_REQUEST, "Secret API 不接受查询参数")
        self._require_secret_host()
        self.send_json(
            HTTPStatus.OK,
            self._secret_status_payload(),
            headers={"X-CSRF-Token": self.server.csrf_token},
        )

    def _validate_secret_payload(self, payload: dict[str, object]) -> tuple[str, str, str, bool]:
        provider_id = payload.get("provider_id")
        action = payload.get("action")
        if not isinstance(provider_id, str) or provider_id not in self.server.provider_secret_refs:
            raise SecretApiError(HTTPStatus.BAD_REQUEST, "Provider 无效")
        if action not in {"set", "rotate", "verify", "delete"}:
            raise SecretApiError(HTTPStatus.BAD_REQUEST, "Secret 操作无效")
        expected_keys = {
            "set": {"provider_id", "action", "request_id", "secret", "connect", "confirmation"},
            "rotate": {"provider_id", "action", "request_id", "secret", "connect", "confirmation"},
            "verify": {"provider_id", "action", "request_id", "connect", "confirmation"},
            "delete": {"provider_id", "action", "request_id", "confirmation"},
        }[action]
        if set(payload) != expected_keys:
            raise SecretApiError(HTTPStatus.BAD_REQUEST, "请求字段无效")
        self.server.reserve_request_id(payload["request_id"])
        connect = payload.get("connect", False)
        if not isinstance(connect, bool):
            raise SecretApiError(HTTPStatus.BAD_REQUEST, "connect 无效")
        confirmations = {
            ("set", False): "SET",
            ("set", True): "CONNECT",
            ("rotate", False): "ROTATE",
            ("rotate", True): "ROTATE_CONNECT",
            ("verify", False): "VERIFY",
            ("verify", True): "CONNECT",
            ("delete", False): "DELETE",
        }
        if payload.get("confirmation") != confirmations[(action, connect)]:
            raise SecretApiError(HTTPStatus.FORBIDDEN, "操作未获得明确确认")
        return provider_id, action, self.server.provider_secret_refs[provider_id], connect

    def _handle_secret_post(self, request: object) -> None:
        if getattr(request, "query", ""):
            raise SecretApiError(HTTPStatus.BAD_REQUEST, "Secret API 不接受查询参数")
        self._require_secret_host()
        self._require_secret_origin()
        self._require_csrf_token()
        payload = self._read_secret_json()
        provider_id, action, secret_ref, connect = self._validate_secret_payload(payload)
        store = self.server.secret_store
        if action in {"set", "rotate", "delete"} and not store.capabilities.persistent:
            raise SecretApiError(HTTPStatus.CONFLICT, "当前 Secret 后端仅支持进程注入")
        verifier = None
        if connect:
            verifier = self.server.provider_verifiers.get(provider_id)
            if verifier is None:
                raise SecretApiError(HTTPStatus.CONFLICT, "Provider 不支持连接验证")
        try:
            if action == "set":
                store.set(secret_ref, payload["secret"], verifier=verifier)
            elif action == "rotate":
                store.rotate(secret_ref, payload["secret"], verifier=verifier)
            elif action == "verify":
                store.verify(secret_ref, verifier=verifier)
            else:
                store.delete(secret_ref)
        except SecretValidationError:
            if action == "verify" or connect:
                record_provider_secret_event(
                    self.server.health_state_path,
                    provider_id,
                    "invalid",
                    operation=action,
                    validation_scope="connection" if connect else "local",
                )
            raise SecretApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "Secret 验证失败") from None
        except SecretNotFound:
            raise SecretApiError(HTTPStatus.CONFLICT, "Secret 尚未配置") from None
        except SecretOperationUnsupported:
            raise SecretApiError(HTTPStatus.CONFLICT, "Secret 操作与当前状态冲突") from None
        except SecretAccessDenied:
            raise SecretApiError(HTTPStatus.FORBIDDEN, "SecretStore 拒绝当前运行账户") from None
        except SecretStoreUnavailable:
            raise SecretApiError(HTTPStatus.SERVICE_UNAVAILABLE, "SecretStore 不可用") from None
        except SecretStoreError:
            raise SecretApiError(HTTPStatus.BAD_REQUEST, "Secret 操作失败") from None
        event_status = {
            "set": "valid" if connect else "configured",
            "rotate": "valid" if connect else "rotated",
            "verify": "valid",
            "delete": "deleted",
        }[action]
        record_provider_secret_event(
            self.server.health_state_path,
            provider_id,
            event_status,
            operation=action,
            validation_scope=("connection" if connect else "local") if action == "verify" or connect else None,
        )
        self.send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "operation": action,
                "provider": provider_secret_status(
                    store, provider_id, secret_ref, self.server.health_state_path
                ),
            },
        )

    def do_GET(self) -> None:
        request = urlparse(self.path)
        path = request.path
        if path == SECRET_API_PATH:
            try:
                self._handle_secret_get(request)
            except SecretApiError as error:
                self.send_json(error.status, {"ok": False, "error": str(error)})
            except (OSError, SecretStoreError):
                self.send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"ok": False, "error": "Secret 状态服务不可用"},
                )
            return
        if path == OPERATIONS_API_PATH:
            if request.query:
                self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "运维配置接口不接受查询参数"})
                return
            try:
                self.send_json(
                    HTTPStatus.OK,
                    self._operations_config_payload(),
                    headers={"X-CSRF-Token": self.server.csrf_token},
                )
            except (OSError, SecretStoreError, ValueError):
                self.send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"ok": False, "error": "运维配置服务不可用"},
                )
            return
        if path in OPERATIONS_ASSETS:
            asset_path = self.server.operations_paths[path]
            content_type = OPERATIONS_ASSETS[path][1]
            try:
                self.send_bytes(HTTPStatus.OK, content_type, asset_path.read_bytes())
            except OSError as error:
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(error)})
            return
        if path in {"/", "/dashboard.html"}:
            try:
                self.send_bytes(HTTPStatus.OK, "text/html; charset=utf-8", self.server.dashboard_path.read_bytes())
            except OSError as error:
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(error)})
            return
        if path == "/api/state":
            try:
                database = connect(self.server.database_path)
                try:
                    payload = state_payload(database, self.server.runtime_config)
                    payload["services"], payload["health_events"] = runtime_health()
                    payload["runtime_config"] = self.server.runtime_config
                    self.send_json(HTTPStatus.OK, payload)
                finally:
                    database.close()
            except (sqlite3.Error, OSError, ValueError) as error:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": str(error)})
            return
        if path == "/api/attachment":
            parameters = parse_qs(request.query, keep_blank_values=True)
            task_ids = parameters.get("task_id", [])
            attachment_paths = parameters.get("path", [])
            if len(task_ids) != 1 or len(attachment_paths) != 1 or not task_ids[0] or not attachment_paths[0]:
                self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "task_id and path are required"})
                return
            try:
                database = connect(self.server.database_path)
                try:
                    image_path, content_type = resolve_attachment_image(
                        database,
                        task_ids[0],
                        attachment_paths[0],
                    )
                    body = image_path.read_bytes()
                finally:
                    database.close()
                self.send_bytes(HTTPStatus.OK, content_type, body)
            except FileNotFoundError as error:
                self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(error)})
            except PermissionError as error:
                self.send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": str(error)})
            except ValueError as error:
                self.send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"ok": False, "error": str(error)})
            except (sqlite3.Error, OSError) as error:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": str(error)})
            return
        if path == "/healthz":
            try:
                database = connect(self.server.database_path)
                try:
                    schema = schema_version(database)
                    active = database.execute("SELECT count(*) FROM executions WHERE status='RUNNING'").fetchone()[0]
                    tasks = database.execute("SELECT count(*) FROM tasks").fetchone()[0]
                    if schema != SCHEMA_VERSION:
                        raise RuntimeError(f"schema_version={schema}, expected={SCHEMA_VERSION}")
                    self.send_json(
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "component": "dashboard-server",
                            "schema_version": schema,
                            "tasks": tasks,
                            "active_executions": active,
                            "checked_at": now_shanghai(),
                        },
                    )
                finally:
                    database.close()
            except (sqlite3.Error, OSError, RuntimeError) as error:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": str(error)})
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        request = urlparse(self.path)
        if request.path == SECRET_API_PATH:
            try:
                self._handle_secret_post(request)
            except SecretApiError as error:
                self.send_json(error.status, {"ok": False, "error": str(error)})
            except (OSError, SecretStoreError):
                self.send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"ok": False, "error": "Secret 状态服务不可用"},
                )
            return
        if request.path == OPERATIONS_ACTION_PATH:
            try:
                self._handle_operations_action(request)
            except (OperationsApiError, SecretApiError) as error:
                self.send_json(error.status, {"ok": False, "error": str(error)})
            except (sqlite3.Error, OSError, ValueError):
                self.send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"ok": False, "error": "运维配置服务不可用"},
                )
            return
        if request.path != TASK_ACTION_PATH:
            self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self.send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"ok": False, "error": "Content-Type 必须为 application/json"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", ""))
            if content_length < 1 or content_length > MAX_ACTION_BODY_BYTES:
                raise ValueError
            payload = json.loads(self.rfile.read(content_length).decode("utf-8", errors="strict"))
            if not isinstance(payload, dict):
                raise DashboardActionError(
                    HTTPStatus.BAD_REQUEST,
                    "请求 JSON 必须为对象",
                )
            action = payload.get("action")
            if action == "archive":
                if set(payload) != {"task_id", "action", "row_version"}:
                    raise DashboardActionError(
                        HTTPStatus.BAD_REQUEST,
                        "归档请求字段无效",
                    )
                result = archive_dashboard_task(
                    self.server.database_path,
                    payload["task_id"],
                    action,
                    payload["row_version"],
                )
            elif action == "recover":
                if set(payload) != {
                    "task_id", "action", "execution_id", "recovery_action", "row_version", "confirmed_safe"
                }:
                    raise DashboardActionError(HTTPStatus.BAD_REQUEST, "恢复请求字段无效")
                result = recover_dashboard_task(
                    self.server.database_path,
                    payload["task_id"],
                    payload["execution_id"],
                    payload["recovery_action"],
                    payload["row_version"],
                    payload["confirmed_safe"],
                )
            else:
                raise DashboardActionError(HTTPStatus.BAD_REQUEST, "action 无效")
            self.send_json(HTTPStatus.OK, result)
        except DashboardActionError as error:
            self.send_json(error.status, {"ok": False, "error": str(error)})
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "请求 JSON 无效"})
        except (sqlite3.Error, OSError) as error:
            self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": str(error)})

    def do_OPTIONS(self) -> None:
        if urlparse(self.path).path in {SECRET_API_PATH, OPERATIONS_ACTION_PATH}:
            self.send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "CORS 请求已拒绝"})
            return
        self.send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"ok": False, "error": "method not allowed"})


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Local Agent Loop SQLite dashboard server")
    value.add_argument("--db", default=str(DEFAULT_DB))
    value.add_argument("--config", default=str(CONFIG_PATH))
    value.add_argument("--host")
    value.add_argument("--port", type=int)
    return value


def main() -> None:
    args = parser().parse_args()
    database_path = Path(args.db).resolve()
    config_path = Path(args.config).resolve()
    config = load_initialization_config(config_path)
    dashboard_config = config["dashboard"]
    host = args.host or str(dashboard_config["host"])
    port = args.port or int(dashboard_config["port"])
    if host != "127.0.0.1":
        raise SystemExit("Dashboard Server with Secret API must bind to 127.0.0.1")
    runtime = BASE_DIR / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    pid_path = runtime / "dashboard-server.pid"
    server = DashboardServer(
        (host, port), database_path, BASE_DIR / "dashboard.html", config,
        runtime_config_path=config_path,
    )
    pid_path.write_text(str(os.getpid()), encoding="utf-8")

    def stop_server(signum: int, frame: object) -> None:
        del signum, frame
        # shutdown() must run outside the serve_forever thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    print(f"{now_shanghai()} dashboard server listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        if pid_path.exists() and pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            pid_path.unlink()


if __name__ == "__main__":
    main()
