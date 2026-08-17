"""集中定义 Supervisor 使用的项目路径和运行文件路径。"""

from __future__ import annotations

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = REPOSITORY_ROOT / "control"
RUNTIME_DIR = REPOSITORY_ROOT / "runtime"
RUNNERS_DIR = RUNTIME_DIR / "runners"
HEALTH_LOCK = RUNTIME_DIR / "health-supervisor.lock"
HEALTH_STATE = RUNTIME_DIR / "health-state.json"
PID_PATH = RUNTIME_DIR / "supervisor.pid"
HEARTBEAT_PATH = RUNTIME_DIR / "supervisor-heartbeat.json"
FALLBACK_LOG = RUNTIME_DIR / "health-fallback.log"
SERVER_LOG = RUNTIME_DIR / "supervisor.log"
