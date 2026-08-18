"""仅支持 Windows 的 Supervisor 单次健康检查入口。

脚本由计划任务周期调用，不常驻轮询。探活、进程恢复和运行文件方法集中在
根目录 ``common``；本文件只保留参数解析和一次健康检查的决策流程。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any


# 健康任务和它启动的后台服务都不生成 __pycache__。
sys.dont_write_bytecode = True

# 计划任务直接运行本文件时，先建立项目根目录和控制面模块搜索路径。
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = REPOSITORY_ROOT / "control"
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from loopdb import BASE_DIR, CONFIG_PATH, DEFAULT_DB, load_initialization_config
from common.health import (
    acquire_lock,
    append_fallback,
    is_supervisor_process,
    output,
    read_state,
    read_supervisor_heartbeat,
    record,
    recorded_pid,
    release_lock,
    start_server,
    supervisor_health,
    write_state,
)
from common.paths import (
    FALLBACK_LOG,
    HEALTH_LOCK,
    HEALTH_STATE,
    HEARTBEAT_PATH,
    PID_PATH,
    RUNTIME_DIR,
    SERVER_LOG,
)
from common.windows import process_alive
from common.service_control import service_control_state


def main(argv: list[str] | None = None) -> None:
    """执行一次探活、必要恢复、重试验证和阈值告警流程。"""
    parser = argparse.ArgumentParser(description="Local Agent Loop Supervisor health check")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args(argv)
    database_path = Path(args.db).resolve()
    config_path = Path(args.config).resolve()

    acquire_lock()
    try:
        config = load_initialization_config(config_path)
        if not service_control_state()["supervisor"]:
            record("DISABLED", "Supervisor 已由人工停止。", 0)
            output({"outcome": "DISABLED", "message": "Supervisor 已由人工停止。"})
        threshold = int(config["health"]["failure_threshold"])
        heartbeat_timeout = int(config["health"]["heartbeat_timeout_seconds"])
        pid, heartbeat = supervisor_health(heartbeat_timeout)

        if pid is not None:
            record("HEALTHY", "Supervisor 主进程正常。", 0, pid)
            output({"outcome": "HEALTHY", "pid": pid, "heartbeat": heartbeat})

        state = read_state()
        failures = int(state.get("consecutive_failures", 0)) + 1
        try:
            pid = start_server(database_path, config_path)
        except (OSError, RuntimeError) as error:
            status = "NEEDS_ATTENTION" if failures >= threshold else "UNHEALTHY"
            message = f"Supervisor 主进程恢复启动失败：{error}"
            record(status, message, failures)
            output(
                {
                    "outcome": status,
                    "message": message,
                    "consecutive_failures": failures,
                    "threshold": threshold,
                },
                2 if status == "NEEDS_ATTENTION" else 1,
            )

        recovered_pid = None
        recovered_heartbeat: dict[str, Any] | None = None
        for _ in range(60):
            time.sleep(0.5)
            recovered_pid, recovered_heartbeat = supervisor_health(
                heartbeat_timeout,
                expected_pid=pid,
            )
            if recovered_pid is not None:
                break

        if recovered_pid is not None:
            record("RESTARTED", "Supervisor 主进程已启动或恢复。", 0, pid)
            output(
                {
                    "outcome": "RESTARTED",
                    "pid": pid,
                    "heartbeat": recovered_heartbeat,
                }
            )

        status = "NEEDS_ATTENTION" if failures >= threshold else "UNHEALTHY"
        message = (
            "Supervisor 主进程连续恢复失败，已达到告警阈值。"
            if status == "NEEDS_ATTENTION"
            else "Supervisor 主进程启动后仍不可用。"
        )
        record(status, message, failures, pid)
        output(
            {
                "outcome": status,
                "pid": pid,
                "consecutive_failures": failures,
                "threshold": threshold,
            },
            2 if status == "NEEDS_ATTENTION" else 1,
        )
    finally:
        # output() 通过 SystemExit 结束流程时也必须释放互斥锁。
        release_lock()


if __name__ == "__main__":
    main()
