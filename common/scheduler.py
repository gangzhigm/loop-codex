"""Planner 与 Dispatcher 常驻 Scheduler 共用的运行文件生命周期。"""

from __future__ import annotations

import os
import signal
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loopdb import now_shanghai

from common.files import heartbeat_belongs_to, read_pid, write_json_atomic
from common.paths import REPOSITORY_ROOT


@dataclass(frozen=True)
class SchedulerRuntimeFiles:
    """冻结一个 Scheduler 的 PID、heartbeat 和停止请求路径。"""

    component: str
    pid_path: Path
    heartbeat_path: Path
    stop_path: Path

    @classmethod
    def from_config(
        cls, config: dict[str, Any], component: str
    ) -> "SchedulerRuntimeFiles":
        """从 Supervisor 组件契约解析项目内运行文件路径。"""
        raw = config["supervisor"]["components"][component]
        return cls(
            component=component,
            pid_path=(REPOSITORY_ROOT / str(raw["pid_path"])).resolve(),
            heartbeat_path=(REPOSITORY_ROOT / str(raw["heartbeat_path"])).resolve(),
            stop_path=(REPOSITORY_ROOT / str(raw["stop_path"])).resolve(),
        )

    def prepare(self) -> None:
        """创建运行目录，并清除上一个已结束实例遗留的停止请求。"""
        self.pid_path.parent.mkdir(parents=True, exist_ok=True)
        self.stop_path.unlink(missing_ok=True)

    def claim(self, pid: int, conflict_message: str) -> None:
        """以排他创建 PID 文件的方式取得当前 Scheduler 单实例所有权。"""
        try:
            descriptor = os.open(
                self.pid_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError:
            raise SystemExit(conflict_message) from None
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(str(pid))

    def write_heartbeat(self, pid: int) -> None:
        """原子发布 Scheduler 当前进程身份和推进时间。"""
        write_json_atomic(
            self.heartbeat_path,
            {
                "component": self.component,
                "pid": pid,
                "status": "RUNNING",
                "checked_at": now_shanghai(),
            },
        )

    def stop_requested(self) -> bool:
        """判断 Supervisor 是否已写入当前 Scheduler 的正常停止请求。"""
        return self.stop_path.exists()

    def cleanup(self, pid: int) -> None:
        """只清理仍属于当前 Scheduler PID 的运行文件。"""
        if read_pid(self.pid_path) == pid:
            self.pid_path.unlink(missing_ok=True)
        if heartbeat_belongs_to(self.heartbeat_path, pid):
            self.heartbeat_path.unlink(missing_ok=True)
        self.stop_path.unlink(missing_ok=True)


def install_shutdown_signals(shutdown_event: threading.Event) -> None:
    """把 Windows 终止和控制台中断信号转换为主循环停止事件。"""

    def stop(signum: int, frame: object) -> None:
        del signum, frame
        shutdown_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
