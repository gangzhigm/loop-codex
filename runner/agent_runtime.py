"""常驻 AI Runner 队列管理入口。

Runner 读取 Planner 与 Worker 的持久 ``QUEUED`` execution，计算可用容量并选出下一批
候选，并按不可变 execution 路由启动 Planner 或正式 AI Worker。任务状态只由 Worker
通过受控 loopctl 领取和写回，Runner 本身不修改任务表。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
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
from common.processes import safe_process_environment
from common.service_runtime import ServiceRuntimeFiles, install_shutdown_signals
from loopdb import CONFIG_PATH, DEFAULT_DB, connect, load_initialization_config, now_shanghai
from runner.validation import ValidationWorkerSettings, is_validation_task


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


def _validation_candidate(candidate: dict[str, Any], description: object, settings: ValidationWorkerSettings) -> bool:
    return (
        candidate["execution_kind"] == "PLANNER"
        and is_validation_task(candidate["task_id"], description, settings)
    )


def queue_snapshot(database_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    """只读两类队列并计算容量；返回值不构成启动授权。"""
    database = connect(database_path)
    try:
        planner_rows = database.execute(
            f"SELECT p.execution_id, p.task_id, p.execution_kind, "
            f"t.runtime_environment, t.provider_id, t.estimated_capability_level AS capability_level, t.description, "
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

    validation_settings = ValidationWorkerSettings.from_config(config)
    planner_maximum = int(config["planner"]["max_active_executions"])
    global_maximum = int(config["task_execution"]["global_max_active_executions"])
    platform_maxima = config["task_execution"]["platform_max_active_executions"]
    platform_active = {row["runtime_environment"]: int(row["active"]) for row in platform_active_rows}
    planner_queued = len(planner_rows)
    planner_available_slots = max(
        0, planner_maximum - planner_queued - planner_active
    )
    planner_launch_slots = max(0, planner_maximum - planner_active)
    worker_slots = max(0, global_maximum - worker_active)
    planner_candidates = []
    for row in planner_rows[:planner_launch_slots]:
        candidate = _candidate(row)
        candidate.update(
            runtime_environment=config["planner"]["worker_runtime_environment"],
            provider_id=config["planner"]["worker_provider_id"],
            capability_level=config["planner"]["capability_level"],
        )
        candidate["validation_eligible"] = _validation_candidate(
            candidate, row["description"], validation_settings
        )
        planner_candidates.append(candidate)
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
        "launch_enabled": bool(config["runner"]["worker_launch_enabled"]),
        "validation_worker": {
            "enabled": validation_settings.enabled,
            "planner_ready_delay_seconds": validation_settings.planner_ready_delay_seconds,
        },
        "planner": {
            "queued": planner_queued,
            "active": planner_active,
            "maximum": planner_maximum,
            "available_slots": planner_available_slots,
            "launch_slots": planner_launch_slots,
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


def _launch_validation_workers(
    snapshot: dict[str, Any],
    database_path: Path,
    config_path: Path,
    active_children: dict[str, subprocess.Popen[str]],
) -> None:
    """只为快照中已标记的 Planner 验证任务启动一次确定性子进程。"""
    for execution_id, process in list(active_children.items()):
        return_code = process.poll()
        if return_code is None:
            continue
        stdout, _stderr = process.communicate()
        active_children.pop(execution_id, None)
        print(
            json.dumps(
                {
                    "event": "runner.validation_worker.exited",
                    "execution_id": execution_id,
                    "return_code": return_code,
                    "result": stdout.strip(),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    if not bool(snapshot.get("launch_enabled")):
        return
    planner = snapshot.get("planner")
    candidates = planner.get("candidates") if isinstance(planner, dict) else []
    if not isinstance(candidates, list):
        return
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("validation_eligible") is not True:
            continue
        execution_id = candidate.get("execution_id")
        task_id = candidate.get("task_id")
        if not isinstance(execution_id, str) or not isinstance(task_id, str) or execution_id in active_children:
            continue
        process = subprocess.Popen(
            [
                sys.executable,
                "-X",
                "utf8",
                "-B",
                str(REPOSITORY_ROOT / "runner" / "verification_worker.py"),
                "--db",
                str(database_path),
                "--config",
                str(config_path),
                "--execution-id",
                execution_id,
                "--task-id",
                task_id,
            ],
            cwd=REPOSITORY_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        active_children[execution_id] = process
        print(
            json.dumps(
                {
                    "event": "runner.validation_worker.started",
                    "execution_id": execution_id,
                    "task_id": task_id,
                    "pid": process.pid,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


def _worker_command(
    candidate: dict[str, Any], database_path: Path, config_path: Path, config: dict[str, Any]
) -> list[str]:
    """把不可变队列候选解析为唯一 Worker 子进程入口。"""
    execution_id = str(candidate["execution_id"])
    execution_kind = str(candidate["execution_kind"])
    capability_level = str(candidate["capability_level"])
    runtime_environment = str(candidate["runtime_environment"])
    provider_id = candidate.get("provider_id")
    common = [
        "--execution-id",
        execution_id,
        "--config",
        str(config_path),
        "--db",
        str(database_path),
    ]
    if execution_kind == "PLANNER":
        if runtime_environment != "codex_cli" or provider_id is not None:
            raise RuntimeError("unsupported Planner Worker route")
        return [
            sys.executable,
            "-X",
            "utf8",
            "-B",
            str(REPOSITORY_ROOT / "worker" / "planner_codex_runtime.py"),
            *common,
        ]
    if execution_kind != "WORKER":
        raise RuntimeError("unsupported execution kind")
    if runtime_environment == "codex_cli" and provider_id is None:
        return [
            sys.executable,
            "-X",
            "utf8",
            "-B",
            str(REPOSITORY_ROOT / "worker" / "codex_cli_runtime.py"),
            *common,
            "--capability-level",
            capability_level,
        ]
    if runtime_environment == "self_hosted_agent" and isinstance(provider_id, str):
        factories = config["self_hosted_agent"]["provider_factories"]
        provider = factories.get(provider_id)
        if not isinstance(provider, str) or not provider:
            raise RuntimeError("self-hosted Worker provider is not configured")
        return [
            sys.executable,
            "-X",
            "utf8",
            "-B",
            str(REPOSITORY_ROOT / "worker" / "agent_runtime.py"),
            "--runtime-environment",
            runtime_environment,
            "--provider-id",
            provider_id,
            "--capability-level",
            capability_level,
            "--execution-policy",
            "automatic",
            "--provider",
            provider,
            *common,
        ]
    raise RuntimeError("unsupported Worker route")


def _reap_children(active_children: dict[str, subprocess.Popen[str]]) -> None:
    for execution_id, process in list(active_children.items()):
        return_code = process.poll()
        if return_code is None:
            continue
        active_children.pop(execution_id, None)
        print(
            json.dumps(
                {
                    "event": "runner.worker.exited",
                    "execution_id": execution_id,
                    "return_code": return_code,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


def _launch_ai_workers(
    snapshot: dict[str, Any],
    database_path: Path,
    config_path: Path,
    config: dict[str, Any],
    active_children: dict[str, subprocess.Popen[str]],
) -> None:
    """按 Runner 快照启动 Planner 或正式 Worker，不修改任务状态。"""
    _reap_children(active_children)
    if not bool(snapshot.get("launch_enabled")):
        return
    validation_enabled = bool(
        (snapshot.get("validation_worker") or {}).get("enabled")
    )
    for queue_name in ("planner", "worker"):
        queue = snapshot.get(queue_name)
        candidates = queue.get("candidates") if isinstance(queue, dict) else []
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if validation_enabled and candidate.get("validation_eligible") is True:
                continue
            execution_id = candidate.get("execution_id")
            if not isinstance(execution_id, str) or execution_id in active_children:
                continue
            command = _worker_command(candidate, database_path, config_path, config)
            process = subprocess.Popen(
                command,
                cwd=REPOSITORY_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=None,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=safe_process_environment(),
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            active_children[execution_id] = process
            print(
                json.dumps(
                    {
                        "event": "runner.worker.started",
                        "execution_id": execution_id,
                        "task_id": candidate.get("task_id"),
                        "execution_kind": candidate.get("execution_kind"),
                        "runtime_environment": candidate.get("runtime_environment"),
                        "capability_level": candidate.get("capability_level"),
                        "pid": process.pid,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )


def _snapshot_signature(snapshot: dict[str, Any]) -> str:
    stable = {**snapshot, "checked_at": None}
    return json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_queue_state(snapshot: dict[str, Any], pid: int) -> None:
    write_json_atomic(RUNNER_QUEUE_STATE, {**snapshot, "pid": pid})


def _clear_queue_state(pid: int) -> None:
    value = read_json_object(RUNNER_QUEUE_STATE)
    if value is None or value.get("pid") == pid:
        RUNNER_QUEUE_STATE.unlink(missing_ok=True)


def _stop_children(
    children: dict[str, subprocess.Popen[str]], grace_seconds: float
) -> None:
    """只停止当前 Runner 明确持有的 Worker 进程组。"""
    for process in children.values():
        if process.poll() is None:
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
            except (OSError, ValueError):
                process.terminate()
    deadline = max(0.1, grace_seconds)
    for process in children.values():
        if process.poll() is not None:
            continue
        try:
            process.wait(timeout=deadline)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=deadline)


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
    validation_children: dict[str, subprocess.Popen[str]] = {}
    ai_children: dict[str, subprocess.Popen[str]] = {}
    try:
        install_shutdown_signals(shutdown_event)
        print(f"{now_shanghai()} runner queue manager started", flush=True)
        while not shutdown_event.is_set():
            if runtime.stop_requested(pid):
                break
            config = load_initialization_config(config_path)
            snapshot = queue_snapshot(database_path, config)
            _launch_validation_workers(snapshot, database_path, config_path, validation_children)
            _launch_ai_workers(
                snapshot, database_path, config_path, config, ai_children
            )
            _write_queue_state(snapshot, pid)
            signature = _snapshot_signature(snapshot)
            if signature != last_signature:
                print(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), flush=True)
                last_signature = signature
            runtime.write_heartbeat(pid)
            runtime.wait(shutdown_event, pid, min(interval, heartbeat_interval))
    finally:
        grace = float(config["runner"]["child_termination_grace_seconds"])
        _stop_children(validation_children, grace)
        _stop_children(ai_children, grace)
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
