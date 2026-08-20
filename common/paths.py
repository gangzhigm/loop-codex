"""集中定义常驻服务使用的项目路径和运行文件路径。"""

from __future__ import annotations

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = REPOSITORY_ROOT / "control"
RUNTIME_DIR = REPOSITORY_ROOT / "data" / "runtime"
RUNNERS_DIR = RUNTIME_DIR / "runners"
RUNNER_QUEUE_STATE = RUNTIME_DIR / "runner-queue-state.json"
HEALTH_LOCK = RUNTIME_DIR / "health-supervisor.lock"
HEALTH_STATE = RUNTIME_DIR / "health-state.json"
PID_PATH = RUNTIME_DIR / "supervisor.pid"
HEARTBEAT_PATH = RUNTIME_DIR / "supervisor-heartbeat.json"
SUPERVISOR_STOP_REQUEST = RUNTIME_DIR / "supervisor-stop-request.json"
FALLBACK_LOG = RUNTIME_DIR / "health-fallback.log"
SERVER_LOG = RUNTIME_DIR / "supervisor.log"
SERVICE_CONTROL = RUNTIME_DIR / "service-control.json"
