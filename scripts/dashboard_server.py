from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.dont_write_bytecode = True

from loopdb import (
    BASE_DIR,
    CONFIG_PATH,
    DEFAULT_DB,
    SCHEMA_VERSION,
    connect,
    load_initialization_config,
    metadata,
    now_shanghai,
    state_payload,
)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        database_path: Path,
        dashboard_path: Path,
        runtime_config: dict[str, object],
    ):
        super().__init__(address, DashboardHandler)
        self.database_path = database_path
        self.dashboard_path = dashboard_path
        self.runtime_config = runtime_config


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer

    def log_message(self, format_string: str, *args: object) -> None:
        if urlparse(self.path).path == "/api/state" and len(args) > 1 and str(args[1]) == "200":
            return
        print(f"{now_shanghai()} {self.client_address[0]} {format_string % args}", flush=True)

    def send_bytes(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, payload: dict[str, object]) -> None:
        self.send_bytes(status, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def do_GET(self) -> None:
        path = urlparse(self.path).path
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
                    payload = state_payload(database)
                    payload["runtime_config"] = self.server.runtime_config
                    self.send_json(HTTPStatus.OK, payload)
                finally:
                    database.close()
            except (sqlite3.Error, OSError, ValueError) as error:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": str(error)})
            return
        if path == "/healthz":
            try:
                database = connect(self.server.database_path)
                try:
                    schema = metadata(database, "schema_version")
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
    runtime = BASE_DIR / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    pid_path = runtime / "dashboard-server.pid"
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    server = DashboardServer((host, port), database_path, BASE_DIR / "dashboard.html", config)

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
