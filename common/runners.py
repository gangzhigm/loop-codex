"""Runner heartbeat 登记和 Supervisor 只读观察协议。"""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from loopdb import now_shanghai

from common.files import read_json_object, write_json_atomic
from common.paths import REPOSITORY_ROOT, RUNNERS_DIR
from common.windows import process_alive, windows_powershell


RUNNER_ENTRIES = {
    ("worker", "self_hosted_agent"): REPOSITORY_ROOT / "runner" / "agent_runtime.py",
}
RUNNER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class RunnerState:
    """维护一个 Runner 拥有的原子状态文件，供 Supervisor 非侵入式读取。"""

    def __init__(
        self,
        runner_id: str,
        mode: str,
        runtime_environment: str,
        execution_id: str,
    ) -> None:
        if not RUNNER_ID_PATTERN.fullmatch(runner_id):
            raise ValueError("Runner ID 只能包含字母、数字、点、下划线和连字符")
        if (mode, runtime_environment) not in RUNNER_ENTRIES:
            raise ValueError("Runner 模式和运行环境组合未登记")
        self.path = RUNNERS_DIR / f"{runner_id}.json"
        self.lock = threading.Lock()
        self.observation_enabled = False
        stamp = now_shanghai()
        self.value: dict[str, Any] = {
            "runner_id": runner_id,
            "mode": mode,
            "runtime_environment": runtime_environment,
            "execution_id": execution_id,
            "runner_pid": os.getpid(),
            "worker_pid": None,
            "status": "RUNNING",
            "started_at": stamp,
            "checked_at": stamp,
        }

    def start(self) -> None:
        """尽力创建初始状态；观察文件故障不得阻止 Runner 执行业务。"""
        try:
            RUNNERS_DIR.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                write_json_atomic(self.path, self.value)
                self.observation_enabled = True
        except OSError:
            return

    def update(self, **fields: object) -> None:
        """更新任务、子进程或阶段信息，并同步刷新观察 heartbeat。"""
        allowed = {"worker_pid", "status"}
        if not set(fields).issubset(allowed):
            raise ValueError("Runner 状态包含不允许更新的字段")
        with self.lock:
            self.value.update(fields)
            self.value["checked_at"] = now_shanghai()
            self._write()

    def touch(self) -> None:
        """在业务 heartbeat 成功后刷新 Runner 的可观察推进时间。"""
        with self.lock:
            self.value["checked_at"] = now_shanghai()
            self._write()

    def close(self) -> None:
        """仅在状态文件仍属于当前 PID 时清理正常结束记录。"""
        with self.lock:
            if not self.observation_enabled:
                return
            try:
                current = read_json_object(self.path)
                if current is not None and current.get("runner_pid") == os.getpid():
                    self.path.unlink(missing_ok=True)
            except OSError:
                return

    def _write(self) -> None:
        """尽力原子替换观察快照，失败时不改变 Runner 业务控制流。"""
        if not self.observation_enabled:
            return
        try:
            write_json_atomic(self.path, self.value)
        except OSError:
            return


class RunnerHeartbeat:
    """仅维护 Runner 自身观察 heartbeat，不参与 Worker 业务事务。"""

    def __init__(self, state: RunnerState, interval_seconds: float) -> None:
        if interval_seconds <= 0:
            raise ValueError("Runner heartbeat interval must be positive")
        self.state = state
        self.interval_seconds = interval_seconds
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def __enter__(self) -> "RunnerHeartbeat":
        self.state.touch()
        self.thread = threading.Thread(
            target=self._run,
            name="runner-observation-heartbeat",
            daemon=True,
        )
        self.thread.start()
        return self

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval_seconds):
            self.state.touch()

    def __exit__(self, *_: Any) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=max(1.0, min(self.interval_seconds, 5.0)))


def _runner_identity_matches(pid: int, mode: str, runtime_environment: str) -> bool:
    """确认 PID 命令行指向状态声明的 Runner 入口。"""
    entry = RUNNER_ENTRIES.get((mode, runtime_environment))
    if entry is None:
        return False
    command_line = windows_powershell(
        f'(Get-CimInstance Win32_Process -Filter "ProcessId = {pid}").CommandLine'
    )
    return bool(command_line) and str(entry).casefold() in command_line.casefold()


def _runner_snapshot(
    path: Path,
    timeout_seconds: float,
) -> dict[str, Any] | None:
    """验证单个 Runner 文件的结构、进程身份和 heartbeat 新鲜度。"""
    checked_at = now_shanghai()
    value = read_json_object(path)
    if value is None:
        # Runner 正常退出可能发生在 glob 与读取之间，此时不制造一次假异常。
        if not path.exists():
            return None
        return {
            "component": f"runner:{path.stem}",
            "runner_id": path.stem,
            "status": "INVALID_STATE",
            "checked_at": checked_at,
            "message": "Runner 状态文件无法解析。",
        }
    try:
        runner_id = str(value["runner_id"])
        mode = str(value["mode"])
        runtime_environment = str(value["runtime_environment"])
        runner_pid = int(value["runner_pid"])
        heartbeat_at = str(value["checked_at"])
        heartbeat_time = datetime.fromisoformat(heartbeat_at)
        current_time = datetime.fromisoformat(checked_at)
        if (
            runner_id != path.stem
            or not RUNNER_ID_PATTERN.fullmatch(runner_id)
            or (mode, runtime_environment) not in RUNNER_ENTRIES
            or runner_pid <= 0
            or heartbeat_time.tzinfo is None
            or current_time.tzinfo is None
        ):
            raise ValueError("timezone missing")
    except (KeyError, TypeError, ValueError):
        return {
            "component": f"runner:{path.stem}",
            "runner_id": path.stem,
            "status": "INVALID_STATE",
            "checked_at": checked_at,
            "message": "Runner 状态字段缺失或无效。",
        }

    result = {
        "component": f"runner:{runner_id}",
        "runner_id": runner_id,
        "mode": mode,
        "runtime_environment": runtime_environment,
        "execution_id": str(value.get("execution_id") or ""),
        "pid": runner_pid,
        "worker_pid": value.get("worker_pid"),
        "runner_status": str(value.get("status") or ""),
        "started_at": value.get("started_at"),
        "heartbeat": {"checked_at": heartbeat_at},
        "checked_at": checked_at,
    }
    if not process_alive(runner_pid):
        result.update(status="PROCESS_MISSING", message="Runner PID 已不存在。")
        return result
    if not _runner_identity_matches(runner_pid, mode, runtime_environment):
        result.update(status="IDENTITY_MISMATCH", message="Runner PID 的命令行身份不匹配。")
        return result
    age = (current_time - heartbeat_time).total_seconds()
    if age < 0 or age > timeout_seconds:
        result.update(status="STALE", message="Runner heartbeat 已超时或时间位于未来。")
        return result
    worker_pid = value.get("worker_pid")
    if worker_pid is not None:
        try:
            if not process_alive(int(worker_pid)):
                result.update(status="WORKER_MISSING", message="Runner 记录的 AI 子进程已不存在。")
                return result
        except (TypeError, ValueError):
            result.update(status="INVALID_STATE", message="Runner 记录的 AI 子进程 PID 无效。")
            return result
    result.update(status="HEALTHY", message="Runner 正常运行。")
    return result


def runner_snapshot(
    timeout_seconds: float,
) -> dict[str, Any]:
    """扫描全部动态 Runner，返回只读汇总和逐实例状态。"""
    checked_at = now_shanghai()
    paths = sorted(RUNNERS_DIR.glob("*.json")) if RUNNERS_DIR.exists() else []
    instances = [
        item
        for path in paths
        if (item := _runner_snapshot(path, timeout_seconds)) is not None
    ]
    unhealthy = [item for item in instances if item["status"] != "HEALTHY"]
    active_count = sum(
        item["status"] not in {"PROCESS_MISSING", "INVALID_STATE"}
        for item in instances
    )
    if unhealthy:
        status = "UNHEALTHY"
        message = f"{len(unhealthy)} 个 Runner 状态异常。"
    elif instances:
        status = "HEALTHY"
        message = f"正在观察 {len(instances)} 个 Runner。"
    else:
        status = "IDLE"
        message = "当前没有活动 Runner。"
    return {
        "component": "runners",
        "status": status,
        "checked_at": checked_at,
        "active_count": active_count,
        "observed_count": len(instances),
        "message": message,
        "instances": instances,
    }
