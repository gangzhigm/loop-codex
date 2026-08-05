from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import secrets
import signal
import sqlite3
import subprocess
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

from loopdb import (
    ARCHIVABLE_STATUSES,
    BASE_DIR,
    CONFIG_PATH,
    DEFAULT_DB,
    SCHEMA_VERSION,
    connect,
    load_initialization_config,
    now_shanghai,
    schema_version,
    state_payload,
)
from deepseek_provider import DeepSeekSettings, verify_deepseek_credential
from secret_store import (
    SecretAccessDenied,
    SecretNotFound,
    SecretOperationUnsupported,
    SecretStore,
    SecretStoreError,
    SecretStoreUnavailable,
    SecretValidationError,
    create_secret_store,
)


HEALTH_STATE = BASE_DIR / "runtime" / "health-state.json"
TASK_ACTION_PATH = "/api/task-action"
SECRET_API_PATH = "/api/secrets"
TASK_ID_PATTERN = re.compile(r"[A-Z][A-Z0-9_-]*\Z")
PROVIDER_ID_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
MAX_ACTION_BODY_BYTES = 4096
SECRET_EVENT_COMPONENT = "provider-secret"
SECRET_EVENT_STATUSES = {
    "configured": "configured_unverified",
    "rotated": "configured_unverified",
    "valid": "valid",
    "invalid": "invalid",
    "deleted": "not_configured",
}
_HEALTH_STATE_LOCK = threading.RLock()
IMAGE_CONTENT_TYPES = {
    ".avif": "image/avif",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class DashboardActionError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


class SecretApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


def provider_secret_refs(config: Mapping[str, Any]) -> dict[str, str]:
    execution_profiles = config.get("execution_profiles")
    self_hosted = execution_profiles.get("self_hosted_agent") if isinstance(execution_profiles, Mapping) else None
    providers = self_hosted.get("providers") if isinstance(self_hosted, Mapping) else None
    if not isinstance(providers, Mapping) or not providers:
        raise ValueError("self-hosted Provider configuration is missing")
    result: dict[str, str] = {}
    for provider_id in providers:
        if not isinstance(provider_id, str) or PROVIDER_ID_PATTERN.fullmatch(provider_id) is None:
            raise ValueError("Provider id is invalid")
        provider_config = config.get(provider_id)
        secret_ref = provider_config.get("secret_ref") if isinstance(provider_config, Mapping) else None
        if not isinstance(secret_ref, str) or not secret_ref:
            raise ValueError(f"Provider {provider_id} secret_ref is missing")
        result[provider_id] = secret_ref
    return result


def connection_verifiers(config: Mapping[str, Any]) -> dict[str, Callable[[str], bool | None]]:
    verifiers: dict[str, Callable[[str], bool | None]] = {}
    if "deepseek" in provider_secret_refs(config):
        settings = DeepSeekSettings.from_config(config)
        verifiers["deepseek"] = lambda candidate: verify_deepseek_credential(candidate, settings)
    return verifiers


def _read_health_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"events": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"events": []}
    return value if isinstance(value, dict) else {"events": []}


def _write_health_state(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.secret-api.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def record_provider_secret_event(
    path: Path,
    provider_id: str,
    event_status: str,
    *,
    operation: str,
    validation_scope: str | None = None,
) -> str:
    if event_status not in SECRET_EVENT_STATUSES:
        raise ValueError("secret event status is invalid")
    recorded_at = now_shanghai()
    event = {
        "at": recorded_at,
        "component": SECRET_EVENT_COMPONENT,
        "status": event_status.upper(),
        "message": "Provider Secret 状态已更新。",
        "details": {
            "provider_id": provider_id,
            "operation": operation,
            "validation_scope": validation_scope,
        },
    }
    with _HEALTH_STATE_LOCK:
        state = _read_health_state(path)
        events = [item for item in (state.get("events") or []) if isinstance(item, dict)]
        state["events"] = [event, *events][:100]
        _write_health_state(path, state)
    return recorded_at


def latest_provider_secret_event(path: Path, provider_id: str) -> dict[str, Any] | None:
    with _HEALTH_STATE_LOCK:
        events = _read_health_state(path).get("events") or []
    for event in events:
        if not isinstance(event, dict) or event.get("component") != SECRET_EVENT_COMPONENT:
            continue
        details = event.get("details")
        if isinstance(details, dict) and details.get("provider_id") == provider_id:
            return event
    return None


def provider_secret_status(
    store: SecretStore,
    provider_id: str,
    secret_ref: str,
    health_state_path: Path,
) -> dict[str, Any]:
    status = store.status(secret_ref)
    event = latest_provider_secret_event(health_state_path, provider_id)
    event_status = str(event.get("status", "")).casefold() if event else ""
    event_details = event.get("details") if event and isinstance(event.get("details"), dict) else {}
    if status.state == "missing":
        public_status, configured = "not_configured", False
    elif status.state == "ready":
        public_status = (
            SECRET_EVENT_STATUSES[event_status]
            if event_status in {"configured", "rotated", "valid", "invalid"}
            else "configured_unverified"
        )
        configured = True
    else:
        public_status, configured = "storage_unavailable", None
    last_validated_at = None
    validation_scope = None
    if event_status in {"valid", "invalid"}:
        last_validated_at = event.get("at")
        validation_scope = event_details.get("validation_scope")
    repair = None
    if status.state == "account_mismatch":
        repair = "请用配置的 SecretStore 账户运行 Dashboard Server；不要把密钥静默复制到其他账户。"
    elif status.state in {"access_denied", "backend_unavailable"}:
        repair = "请检查当前运行账户的系统密钥库会话与权限。"
    return {
        "provider_id": provider_id,
        "configured": configured,
        "backend": status.backend,
        "status": public_status,
        "last_validated_at": last_validated_at,
        "validation_scope": validation_scope,
        "persistent": status.persistent,
        "mutable": status.mutable,
        "repair": repair,
    }


def run_loopctl(database_path: Path, arguments: list[str]) -> dict[str, object]:
    try:
        completed = subprocess.run(
            [sys.executable, str(BASE_DIR / "scripts" / "loopctl.py"), "--db", str(database_path), *arguments],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="strict",
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as error:
        raise DashboardActionError(HTTPStatus.SERVICE_UNAVAILABLE, "任务状态服务执行失败") from error
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as error:
        raise DashboardActionError(HTTPStatus.SERVICE_UNAVAILABLE, "任务状态服务返回无效") from error
    if not isinstance(payload, dict):
        raise DashboardActionError(HTTPStatus.SERVICE_UNAVAILABLE, "任务状态服务返回无效")
    if completed.returncode != 0 or payload.get("outcome") == "ERROR":
        message = payload.get("message")
        raise DashboardActionError(
            HTTPStatus.CONFLICT,
            str(message) if isinstance(message, str) and message else "任务状态已变化，请刷新后重试",
        )
    return payload


def archive_dashboard_task(
    database_path: Path,
    task_id: object,
    action: object,
    row_version: object,
) -> dict[str, object]:
    if not isinstance(task_id, str) or TASK_ID_PATTERN.fullmatch(task_id) is None:
        raise DashboardActionError(HTTPStatus.BAD_REQUEST, "task_id 无效")
    if action != "archive":
        raise DashboardActionError(HTTPStatus.BAD_REQUEST, "action 仅支持 archive")
    if isinstance(row_version, bool) or not isinstance(row_version, int) or row_version < 1:
        raise DashboardActionError(HTTPStatus.BAD_REQUEST, "row_version 无效")

    database = connect(database_path)
    try:
        task = database.execute(
            "SELECT status, archived_at, row_version FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    finally:
        database.close()
    if task is None:
        raise DashboardActionError(HTTPStatus.NOT_FOUND, "任务不存在")
    if task["row_version"] != row_version:
        raise DashboardActionError(HTTPStatus.CONFLICT, "任务状态已变化，请刷新后重试")
    if task["archived_at"] is not None:
        raise DashboardActionError(HTTPStatus.CONFLICT, "任务已经归档，请刷新列表")

    confirmed = False
    expected_row_version = row_version
    if task["status"] == "SUCCEEDED":
        confirmation = run_loopctl(
            database_path,
            [
                "confirm",
                task_id,
                "--reason",
                "Dashboard 人工确认并归档。",
                "--expected-row-version",
                str(expected_row_version),
            ],
        )
        expected_row_version = int(confirmation["row_version"])
        confirmed = True
    elif task["status"] not in ARCHIVABLE_STATUSES:
        raise DashboardActionError(HTTPStatus.CONFLICT, "当前任务状态不允许归档，请刷新后重试")

    archived = run_loopctl(
        database_path,
        [
            "archive",
            task_id,
            "--reason",
            "Dashboard 人工归档任务。",
            "--expected-row-version",
            str(expected_row_version),
        ],
    )
    return {"ok": True, "confirmed": confirmed, **archived}


def runtime_health() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    if not HEALTH_STATE.exists():
        return [], []
    try:
        value = json.loads(HEALTH_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], []
    service = {
        key: value.get(key)
        for key in ("component", "status", "pid", "checked_at", "consecutive_failures", "message")
    }
    services = [service] if service.get("component") else []
    events = [item for item in (value.get("events") or []) if isinstance(item, dict)][:12]
    return services, events


def resolve_attachment_image(
    database: sqlite3.Connection,
    task_id: str,
    attachment_path: str,
    base_dir: Path = BASE_DIR,
) -> tuple[Path, str]:
    registered = database.execute(
        "SELECT 1 FROM task_attachments WHERE task_id=? AND path=?",
        (task_id, attachment_path),
    ).fetchone()
    if registered is None:
        raise FileNotFoundError("attachment not found")

    task_root = (base_dir / "assets" / task_id).resolve()
    image_path = (base_dir / attachment_path).resolve()
    if not image_path.is_relative_to(task_root):
        raise PermissionError("attachment path is outside the task asset directory")

    content_type = IMAGE_CONTENT_TYPES.get(image_path.suffix.lower())
    if content_type is None:
        raise ValueError("attachment is not a supported image")
    if not image_path.is_file():
        raise FileNotFoundError("attachment file not found")
    return image_path, content_type


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
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise SecretApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type 必须为 application/json")
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise SecretApiError(HTTPStatus.BAD_REQUEST, "Content-Length 无效") from None
        if content_length < 1:
            raise SecretApiError(HTTPStatus.BAD_REQUEST, "请求体为空")
        if content_length > self.server.secret_api_max_body_bytes:
            raise SecretApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "请求体超过大小限制")
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
            if not isinstance(payload, dict) or set(payload) != {"task_id", "action", "row_version"}:
                raise DashboardActionError(
                    HTTPStatus.BAD_REQUEST,
                    "请求必须且只能包含 task_id、action 和 row_version",
                )
            result = archive_dashboard_task(
                self.server.database_path,
                payload["task_id"],
                payload["action"],
                payload["row_version"],
            )
            self.send_json(HTTPStatus.OK, result)
        except DashboardActionError as error:
            self.send_json(error.status, {"ok": False, "error": str(error)})
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "请求 JSON 无效"})
        except (sqlite3.Error, OSError) as error:
            self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": str(error)})

    def do_OPTIONS(self) -> None:
        if urlparse(self.path).path == SECRET_API_PATH:
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
    config = load_initialization_config(args.config)
    dashboard_config = config["dashboard"]
    host = args.host or str(dashboard_config["host"])
    port = args.port or int(dashboard_config["port"])
    if host != "127.0.0.1":
        raise SystemExit("Dashboard Server with Secret API must bind to 127.0.0.1")
    runtime = BASE_DIR / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    pid_path = runtime / "dashboard-server.pid"
    server = DashboardServer((host, port), database_path, BASE_DIR / "dashboard.html", config)
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
