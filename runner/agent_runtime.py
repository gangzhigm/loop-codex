"""常驻 AI Runner 队列管理入口。

Runner 读取 Planner 与 Worker 的持久 ``QUEUED`` execution，计算可用容量并选出下一批
候选。当前阶段明确不启动 AI Worker，也不把任何 execution 改为运行中。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Sequence

sys.dont_write_bytecode = True

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = REPOSITORY_ROOT / "control"
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from common.files import read_json_object, write_json_atomic
from common.paths import RUNNER_QUEUE_STATE
from common.service_runtime import ServiceRuntimeFiles, install_shutdown_signals
from loopdb import CONFIG_PATH, DEFAULT_DB, connect, load_initialization_config, now_shanghai


PRIORITY_ORDER = (
    "CASE t.priority WHEN 'blocker' THEN 0 WHEN 'critical' THEN 1 "
    "WHEN 'high' THEN 2 WHEN 'medium' THEN 3 ELSE 4 END"
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Local Agent Loop AI Runner manager")
    commands = value.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="常驻观察 AI execution 队列")
    serve.add_argument("--db", default=str(DEFAULT_DB))
    serve.add_argument("--config", default=str(CONFIG_PATH))
    serve.add_argument("--poll-interval-seconds", type=float)
    return value


def _candidate(row: Any) -> dict[str, Any]:
    return {
        "execution_id": row["execution_id"],
        "task_id": row["task_id"],
        "execution_kind": row["execution_kind"],
        "runtime_environment": row["runtime_environment"],
        "provider_id": row["provider_id"],
        "capability_level": row["capability_level"],
        "queued_at": row["queued_at"],
    }


def queue_snapshot(database_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    """只读两类队列并计算容量；返回值不构成启动授权。"""
    database = connect(database_path)
    try:
        planner_rows = database.execute(
            f"SELECT p.execution_id, p.task_id, p.execution_kind, "
            f"t.runtime_environment, t.provider_id, t.estimated_capability_level AS capability_level, "
            f"p.started_at AS queued_at FROM preflight_executions p JOIN tasks t ON t.id=p.task_id "
            f"WHERE p.status='QUEUED' AND t.status='DRAFT' AND t.preflight_status='QUEUED' "
            f"ORDER BY {PRIORITY_ORDER}, t.created_at, t.id"
        ).fetchall()
        worker_rows = database.execute(
            f"SELECT e.execution_id, e.task_id, e.execution_kind, e.runtime_environment, e.provider_id, "
            f"e.capability_level, e.started_at AS queued_at FROM executions e "
            f"JOIN tasks t ON t.id=e.task_id WHERE e.status='QUEUED' AND t.status='QUEUED' "
            f"ORDER BY {PRIORITY_ORDER}, t.created_at, t.id"
        ).fetchall()
        planner_active = int(
            database.execute(
                "SELECT count(*) FROM preflight_executions WHERE status='INSPECTING'"
            ).fetchone()[0]
        )
        worker_active = int(
            database.execute(
                "SELECT count(*) FROM executions WHERE status='RUNNING'"
            ).fetchone()[0]
        )
        platform_active_rows = database.execute(
            "SELECT runtime_environment, count(*) AS active FROM executions "
            "WHERE status='RUNNING' GROUP BY runtime_environment"
        ).fetchall()
    finally:
        database.close()

    planner_maximum = int(config["planner"]["max_active_executions"])
    global_maximum = int(config["task_execution"]["global_max_active_executions"])
    platform_maxima = config["task_execution"]["platform_max_active_executions"]
    platform_active = {row["runtime_environment"]: int(row["active"]) for row in platform_active_rows}
    planner_slots = max(0, planner_maximum - planner_active)
    worker_slots = max(0, global_maximum - worker_active)
    planner_candidates = [_candidate(row) for row in planner_rows[:planner_slots]]
    platform_slots = {
        environment: max(
            0, int(maximum) - platform_active.get(environment, 0)
        )
        for environment, maximum in platform_maxima.items()
    }
    worker_candidates: list[dict[str, Any]] = []
    for row in worker_rows:
        if len(worker_candidates) >= worker_slots:
            break
        candidate = _candidate(row)
        environment = candidate["runtime_environment"]
        if platform_slots.get(environment, 0) <= 0:
            continue
        worker_candidates.append(candidate)
        platform_slots[environment] -= 1
    return {
        "component": "runner",
        "status": "OBSERVING",
        "checked_at": now_shanghai(),
        "launch_enabled": False,
        "planner": {
            "queued": len(planner_rows),
            "active": planner_active,
            "maximum": planner_maximum,
            "available_slots": planner_slots,
            "candidates": planner_candidates,
        },
        "worker": {
            "queued": len(worker_rows),
            "active": worker_active,
            "maximum": global_maximum,
            "available_slots": worker_slots,
            "platform_active": platform_active,
            "platform_maximum": platform_maxima,
            "candidates": worker_candidates,
        },
    }


def _snapshot_signature(snapshot: dict[str, Any]) -> str:
    stable = {**snapshot, "checked_at": None}
    return json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_queue_state(snapshot: dict[str, Any], pid: int) -> None:
    write_json_atomic(RUNNER_QUEUE_STATE, {**snapshot, "pid": pid})


def _clear_queue_state(pid: int) -> None:
    value = read_json_object(RUNNER_QUEUE_STATE)
    if value is None or value.get("pid") == pid:
        RUNNER_QUEUE_STATE.unlink(missing_ok=True)


def serve_runner(args: argparse.Namespace) -> None:
    config_path = Path(args.config).resolve()
    database_path = Path(args.db).resolve()
    config = load_initialization_config(config_path)
    configured_interval = float(config["runner"]["queue_poll_interval_seconds"])
    if args.poll_interval_seconds is not None and args.poll_interval_seconds < 1:
        raise SystemExit("--poll-interval-seconds 必须至少为 1")
    interval = args.poll_interval_seconds or configured_interval
    heartbeat_interval = float(config["runner"]["heartbeat_interval_seconds"])
    runtime = ServiceRuntimeFiles.from_component_config(config, "runner")
    shutdown_event = threading.Event()
    pid = os.getpid()
    runtime.prepare()
    runtime.claim(pid, "Runner PID 文件已存在")
    last_signature: str | None = None
    try:
        install_shutdown_signals(shutdown_event)
        print(f"{now_shanghai()} runner queue manager started", flush=True)
        while not shutdown_event.is_set():
            if runtime.stop_requested(pid):
                break
            config = load_initialization_config(config_path)
            snapshot = queue_snapshot(database_path, config)
            _write_queue_state(snapshot, pid)
            signature = _snapshot_signature(snapshot)
            if signature != last_signature:
                print(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), flush=True)
                last_signature = signature
            runtime.write_heartbeat(pid)
            runtime.wait(shutdown_event, pid, min(interval, heartbeat_interval))
    finally:
        _clear_queue_state(pid)
        runtime.cleanup(pid)


def main(argv: Sequence[str] | None = None) -> None:
    args = parser().parse_args(argv)
    serve_runner(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            json.dumps(
                {"outcome": "RUNNER_ERROR", "error": f"runner error: {type(error).__name__}"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1)
