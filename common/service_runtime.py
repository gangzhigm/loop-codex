"""三个常驻服务共用的 PID、heartbeat 与停止请求生命周期。"""

from __future__ import annotations

import os
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from loopdb import now_shanghai

from common.files import heartbeat_belongs_to, read_json_object, read_pid, write_json_atomic
from common.paths import (
    HEARTBEAT_PATH,
    PID_PATH,
    REPOSITORY_ROOT,
    SERVER_LOG,
    SUPERVISOR_STOP_REQUEST,
)


def read_service_heartbeat(path: Path) -> dict[str, Any] | None:
    """读取标准服务 heartbeat；缺失、损坏或字段无效时返回 ``None``。"""
    value = read_json_object(path)
    if value is None:
        return None
    try:
        return {
            "component": str(value["component"]),
            "pid": int(value["pid"]),
            "status": str(value["status"]),
            "checked_at": str(value["checked_at"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


def validate_service_heartbeat(
    heartbeat: dict[str, Any] | None,
    *,
    component: str,
    pid: int,
    timeout_seconds: float,
) -> str | None:
    """验证 heartbeat 的身份、状态、时区和新鲜度；正常时返回 ``None``。"""
    if heartbeat is None:
        return "heartbeat 文件缺失或无效。"
    if heartbeat["component"] != component or heartbeat["pid"] != pid:
        return "heartbeat 与组件身份或 PID 不一致。"
    if heartbeat["status"] != "RUNNING":
        return "heartbeat 未声明 RUNNING 状态。"
    try:
        checked_at = datetime.fromisoformat(heartbeat["checked_at"])
        current = datetime.fromisoformat(now_shanghai())
        if checked_at.tzinfo is None or current.tzinfo is None:
            return "heartbeat 时间缺少时区。"
    except (TypeError, ValueError):
        return "heartbeat 时间格式无效。"
    age = (current - checked_at).total_seconds()
    if age < 0 or age > timeout_seconds:
        return "heartbeat 已超时或时间位于未来。"
    return None


@dataclass(frozen=True)
class ServiceRuntimeFiles:
    """冻结一个常驻服务的运行文件位置，并提供所有权安全的读写操作。"""

    component: str
    pid_path: Path
    heartbeat_path: Path
    stop_path: Path
    log_path: Path

    @classmethod
    def from_component_config(
        cls,
        config: dict[str, Any],
        component: str,
    ) -> "ServiceRuntimeFiles":
        """从 Supervisor 组件契约解析 Dashboard 或 Scheduler 运行文件。"""
        raw = config["supervisor"]["components"][component]
        return cls(
            component=component,
            pid_path=(REPOSITORY_ROOT / str(raw["pid_path"])).resolve(),
            heartbeat_path=(REPOSITORY_ROOT / str(raw["heartbeat_path"])).resolve(),
            stop_path=(REPOSITORY_ROOT / str(raw["stop_path"])).resolve(),
            log_path=(REPOSITORY_ROOT / str(raw["log_path"])).resolve(),
        )

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        component: str,
    ) -> "ServiceRuntimeFiles":
        """兼容旧 Scheduler 调用；新代码使用 ``from_component_config``。"""
        return cls.from_component_config(config, component)

    @classmethod
    def supervisor(cls) -> "ServiceRuntimeFiles":
        """构造由 Windows 健康任务管理的 Supervisor 运行文件契约。"""
        return cls(
            component="supervisor",
            pid_path=PID_PATH,
            heartbeat_path=HEARTBEAT_PATH,
            stop_path=SUPERVISOR_STOP_REQUEST,
            log_path=SERVER_LOG,
        )

    def prepare(self) -> None:
        """创建运行目录；停止请求只能在成功取得单实例所有权后清理。"""
        self.pid_path.parent.mkdir(parents=True, exist_ok=True)

    def claim(self, pid: int, conflict_message: str) -> None:
        """以排他创建 PID 文件的方式取得当前服务单实例所有权。"""
        try:
            descriptor = os.open(self.pid_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise SystemExit(conflict_message) from None
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(str(pid))
        self.stop_path.unlink(missing_ok=True)

    def recorded_pid(self) -> int | None:
        """读取当前 PID 文件；内容不可信时返回 ``None``。"""
        return read_pid(self.pid_path)

    def read_heartbeat(self) -> dict[str, Any] | None:
        """读取当前服务的标准 heartbeat。"""
        return read_service_heartbeat(self.heartbeat_path)

    def heartbeat_problem(self, pid: int, timeout_seconds: float) -> str | None:
        """校验当前服务 heartbeat，正常时返回 ``None``。"""
        return validate_service_heartbeat(
            self.read_heartbeat(),
            component=self.component,
            pid=pid,
            timeout_seconds=timeout_seconds,
        )

    def write_heartbeat(self, pid: int) -> None:
        """原子发布当前服务身份、运行状态和推进时间。"""
        write_json_atomic(
            self.heartbeat_path,
            {
                "component": self.component,
                "pid": pid,
                "status": "RUNNING",
                "checked_at": now_shanghai(),
            },
        )

    def request_stop(self, pid: int) -> None:
        """写入仅针对指定 PID 的正常停止请求。"""
        write_json_atomic(
            self.stop_path,
            {"component": self.component, "pid": pid, "requested_at": now_shanghai()},
        )

    def stop_requested(self, pid: int | None = None) -> bool:
        """确认停止请求存在，并在提供 PID 时校验请求仍属于当前实例。"""
        request = read_json_object(self.stop_path)
        if request is None:
            return False
        if pid is None:
            return True
        try:
            return request["component"] == self.component and int(request["pid"]) == pid
        except (KeyError, TypeError, ValueError):
            return False

    def wait(
        self,
        shutdown_event: threading.Event,
        pid: int,
        timeout_seconds: float,
        *,
        poll_interval_seconds: float = 1.0,
    ) -> bool:
        """等待信号或当前实例停止请求；返回是否应结束服务循环。"""
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while not shutdown_event.is_set():
            if self.stop_requested(pid):
                shutdown_event.set()
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            shutdown_event.wait(min(poll_interval_seconds, remaining))
        return True

    def clear(self, pid: int | None) -> None:
        """清理指定已停止实例的文件；``None`` 用于清除确认过的遗留状态。"""
        recorded = self.recorded_pid()
        if pid is None or recorded == pid:
            self.pid_path.unlink(missing_ok=True)
        heartbeat = self.read_heartbeat()
        if pid is None or (heartbeat is not None and heartbeat["pid"] == pid):
            self.heartbeat_path.unlink(missing_ok=True)
        self.stop_path.unlink(missing_ok=True)

    def cleanup(self, pid: int) -> None:
        """退出时只清理仍属于当前服务 PID 的运行文件。"""
        if self.recorded_pid() == pid:
            self.pid_path.unlink(missing_ok=True)
        if heartbeat_belongs_to(self.heartbeat_path, pid):
            self.heartbeat_path.unlink(missing_ok=True)
        request = read_json_object(self.stop_path)
        if request is None or request.get("pid") == pid:
            self.stop_path.unlink(missing_ok=True)


def install_shutdown_signals(shutdown_event: threading.Event) -> None:
    """把 Windows 终止和控制台中断信号转换为主循环停止事件。"""

    def stop(signum: int, frame: object) -> None:
        del signum, frame
        shutdown_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
