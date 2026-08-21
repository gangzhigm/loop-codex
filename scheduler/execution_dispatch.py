"""Scheduler 的正式 Worker execution 排队状态机。

本模块只负责把一个 ``PENDING/READY`` 自动任务原子转换为 ``QUEUED``，并创建对应的
``WORKER/QUEUED`` execution。AI Runner 读取队列，但本模块不启动 Runner 或 Worker。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = REPOSITORY_ROOT / "control"
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))

from loop_agent.control.io import output, require_expected_row_version
from loop_agent.control.queue import requeue_resolved_conflicts
from loopdb import (
    BASE_DIR,
    CAPABILITY_LEVELS,
    CONFIG_PATH,
    DEPENDENCY_COMPLETE_STATUSES,
    LoopError,
    all_tasks,
    bump_revision,
    commit,
    connect,
    expires_at,
    load_initialization_config,
    now_shanghai,
    resolve_execution_profile,
    rollback,
    transaction,
)


class ExecutionDispatchError(RuntimeError):
    """正式 execution 排队配置或事务不符合受控契约。"""


@dataclass(frozen=True)
class ExecutionDispatchSettings:
    """冻结 Scheduler 正式排队链的路由、数据库和周期配置。"""

    config_path: Path
    database_path: Path
    scheduled: bool
    interval_minutes: int
    max_tasks_per_cycle: int
    log_path: Path
    supported_capability_levels: tuple[str, ...]

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        *,
        base_dir: Path = BASE_DIR,
        config_path: Path = CONFIG_PATH,
    ) -> "ExecutionDispatchSettings":
        scheduler = config.get("scheduler")
        raw = scheduler.get("execution") if isinstance(scheduler, dict) else None
        database = config.get("database")
        if not isinstance(raw, dict) or not isinstance(database, dict):
            raise ExecutionDispatchError("scheduler execution configuration is incomplete")
        scheduled = raw.get("scheduled")
        interval = raw.get("interval_minutes")
        maximum_per_cycle = raw.get("max_tasks_per_cycle")
        log_value = raw.get("log_path")
        supported = raw.get("supported_capability_levels")
        if not isinstance(scheduled, bool):
            raise ExecutionDispatchError("scheduler execution scheduled is invalid")
        if not isinstance(interval, int) or interval < 1:
            raise ExecutionDispatchError("scheduler execution interval_minutes is invalid")
        if not isinstance(maximum_per_cycle, int) or maximum_per_cycle < 1:
            raise ExecutionDispatchError("scheduler execution max_tasks_per_cycle is invalid")
        if not isinstance(log_value, str) or not log_value.strip():
            raise ExecutionDispatchError("scheduler execution log_path is invalid")
        if not isinstance(supported, list) or not supported or len(supported) != len(set(supported)):
            raise ExecutionDispatchError("scheduler execution capability levels are invalid")
        if any(level not in CAPABILITY_LEVELS for level in supported):
            raise ExecutionDispatchError("scheduler execution has an unsupported capability level")
        profiles = config.get("execution_profiles") or {}
        if not isinstance(profiles, dict) or not profiles:
            raise ExecutionDispatchError("scheduler execution profiles are missing")
        root = base_dir.resolve()
        database_path = (root / str(database.get("path") or "")).resolve()
        log_path = (root / log_value).resolve()
        if not database_path.is_relative_to(root) or not log_path.is_relative_to(root):
            raise ExecutionDispatchError("scheduler execution paths are unsafe")
        return cls(
            config_path=config_path.resolve(),
            database_path=database_path,
            scheduled=scheduled,
            interval_minutes=interval,
            max_tasks_per_cycle=maximum_per_cycle,
            log_path=log_path,
            supported_capability_levels=tuple(supported),
        )


class EventLogger:
    """只把正式排队结果元数据追加到 UTF-8 JSONL 日志。"""

    def __init__(self, path: Path | None) -> None:
        self.path = path

    def event(self, outcome: str, **fields: object) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"at": now_shanghai(), "outcome": outcome, **fields}
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def dependencies_complete(task: dict[str, Any], statuses: dict[str, str]) -> bool:
    dependencies = task.get("depends_on")
    return isinstance(dependencies, list) and all(
        statuses.get(str(dependency)) in DEPENDENCY_COMPLETE_STATUSES
        for dependency in dependencies
    )


def select_candidate(
    tasks: list[dict[str, Any]],
    provider_id: str | None,
    supported_capability_levels: tuple[str, ...],
    config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """按数据库投影顺序选择首个具备完整执行契约的自动任务。"""
    statuses = {str(task.get("id")): str(task.get("status")) for task in tasks}
    ordered = tasks
    if config is not None:
        levels = config["priority_policy"]["levels"]
        rank = {level: index for index, level in enumerate(levels)}
        ordered = sorted(
            tasks,
            key=lambda task: (
                rank.get(str(task.get("priority")), len(rank)),
                str(task.get("created_at") or ""),
                str(task.get("id") or ""),
            ),
        )
    for task in ordered:
        planner = task.get("planner_supplement") or {}
        if (
            task.get("status") == "PENDING"
            and task.get("preflight_status") == "READY"
            and (provider_id is None or task.get("provider_id") == provider_id)
            and task.get("capability_level") in supported_capability_levels
            and task.get("execution_policy") == "automatic"
            and task.get("lock_mode") is not None
            and bool(task.get("scope"))
            and bool(planner.get("technical_acceptance"))
            and bool(planner.get("evidence"))
            and dependencies_complete(task, statuses)
            and not bool(task.get("blocking_scopes"))
        ):
            if config is not None:
                try:
                    resolve_execution_profile(
                        str(task.get("runtime_environment")),
                        task.get("provider_id"),
                        str(task.get("capability_level")),
                        config,
                    )
                except Exception:
                    continue
            return task
    return None


class ExecutionDispatcher:
    """完成一次候选选择和持久排队，不启动任何 AI 进程。"""

    def __init__(
        self,
        settings: ExecutionDispatchSettings,
        config: dict[str, Any],
        *,
        execution_id_factory: Callable[[str], str] | None = None,
        logger: EventLogger | None = None,
        route_filter: tuple[str, str | None] | None = None,
    ) -> None:
        self.settings = settings
        self.config = config
        self.execution_id_factory = execution_id_factory or (
            lambda level: f"worker-{level.lower()}-{uuid.uuid4()}"
        )
        self.logger = logger or EventLogger(settings.log_path)
        self.route_filter = route_filter

    def _run_one(self) -> dict[str, Any]:
        database = connect(self.settings.database_path)
        try:
            transaction(database)
            requeue_resolved_conflicts(database)
            queued_count = int(
                database.execute(
                    "SELECT count(*) FROM executions WHERE status='QUEUED'"
                ).fetchone()[0]
            )
            queue_maximum = int(
                self.config["task_execution"]["max_queued_executions"]
            )
            if queued_count >= queue_maximum:
                commit(database)
                self.logger.event(
                    "QUEUE_FULL", queued=queued_count, maximum=queue_maximum
                )
                return {
                    "outcome": "QUEUE_FULL",
                    "queued_count": 0,
                    "queued": queued_count,
                    "maximum": queue_maximum,
                }
            candidates = all_tasks(database)
            if self.route_filter is not None:
                candidates = [
                    task
                    for task in candidates
                    if (
                        task.get("runtime_environment"),
                        task.get("provider_id"),
                    )
                    == self.route_filter
                ]
            candidate = select_candidate(
                candidates,
                None,
                self.settings.supported_capability_levels,
                self.config,
            )
            if candidate is None:
                commit(database)
                self.logger.event("NO_TASK")
                return {"outcome": "NO_TASK", "queued_count": 0}
            task_id = str(candidate["id"])
            capability_level = str(candidate["capability_level"])
            runtime_environment = str(candidate["runtime_environment"])
            provider_id = candidate.get("provider_id")
            execution_id = self.execution_id_factory(capability_level)
            if not execution_id or len(execution_id) > 128:
                raise ExecutionDispatchError("generated execution-id is invalid")
            profile = resolve_execution_profile(
                runtime_environment,
                provider_id,
                capability_level,
                self.config,
            )
            stamp = now_shanghai()
            lease = expires_at(int(self.config["task_execution"]["task_lease_seconds"]))
            database.execute(
                """INSERT INTO executions(
                  execution_id, task_id, status, started_at, heartbeat_at, lease_expires_at,
                  runtime_environment, provider_id, capability_level, execution_policy, model, reasoning,
                  attempt_timeout_seconds, max_retries
                ) VALUES(?, ?, 'QUEUED', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    execution_id, task_id, stamp, stamp, lease, runtime_environment,
                    provider_id, capability_level, "automatic", profile["model"],
                    profile["reasoning"], profile["attempt_timeout_seconds"], profile["max_retries"],
                ),
            )
            changed = database.execute(
                "UPDATE tasks SET status='QUEUED', updated_at=?, "
                "progress_summary='Dispatcher 已将任务排入正式 AI 队列。', "
                "progress_next_step='等待 Runner 选择并启动 AI Worker。', "
                "row_version=row_version+1 WHERE id=? AND status='PENDING' "
                "AND preflight_status='READY' AND row_version=?",
                (stamp, task_id, candidate["row_version"]),
            ).rowcount
            if changed != 1:
                raise ExecutionDispatchError("task changed while execution was queued")
            database.execute(
                "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
                "VALUES(?, ?, 'PENDING', 'QUEUED', 'dispatcher', "
                "'Dispatcher 原子创建 WORKER/QUEUED execution。')",
                (task_id, stamp),
            )
            revision = bump_revision(database, "dispatcher")
            commit(database)
            result = {
                "outcome": "QUEUED", "queued_count": 1, "task_id": task_id,
                "execution_id": execution_id, "execution_kind": "WORKER",
                "capability_level": capability_level,
                "runtime_environment": runtime_environment,
                "provider_id": provider_id,
                "revision": revision,
            }
            self.logger.event(
                "QUEUED", **{key: value for key, value in result.items() if key != "outcome"}
            )
            return result
        except Exception:
            rollback(database)
            raise
        finally:
            database.close()

    def run(self) -> dict[str, Any]:
        """按公共单轮上限重复执行独立原子排队事务。"""
        queued: list[dict[str, Any]] = []
        final: dict[str, Any] = {"outcome": "NO_TASK", "queued_count": 0}
        for _ in range(self.settings.max_tasks_per_cycle):
            result = self._run_one()
            if result.get("outcome") != "QUEUED":
                final = result
                break
            queued.append(result)
        if not queued:
            return final
        first = queued[0]
        return {
            **first,
            "queued_count": len(queued),
            "queued": [
                {
                    "task_id": item["task_id"],
                    "execution_id": item["execution_id"],
                    "runtime_environment": item["runtime_environment"],
                    "provider_id": item["provider_id"],
                    "capability_level": item["capability_level"],
                }
                for item in queued
            ],
        }


def command_schedule_execution(args: argparse.Namespace) -> None:
    config_path = Path(args.config).resolve()
    config = load_initialization_config(config_path)
    settings = ExecutionDispatchSettings.from_config(config, config_path=config_path)
    database_path = Path(args.db).resolve()
    output(ExecutionDispatcher(replace(settings, database_path=database_path), config).run())


def command_unschedule_execution(args: argparse.Namespace) -> None:
    """人工撤回尚未领取的正式 Worker execution，并保留 Planner 结果。"""
    database = connect(args.db)
    try:
        transaction(database)
        task = database.execute(
            "SELECT * FROM tasks WHERE id=?",
            (args.task_id,),
        ).fetchone()
        if task is None:
            raise LoopError("任务不存在")
        require_expected_row_version(args, task["row_version"])
        if task["status"] != "QUEUED" or task["preflight_status"] != "READY":
            raise LoopError("只有尚未领取的 QUEUED/READY 任务可以撤回")
        executions = database.execute(
            "SELECT * FROM executions WHERE task_id=? AND status='QUEUED' ORDER BY started_at",
            (args.task_id,),
        ).fetchall()
        if len(executions) != 1:
            raise LoopError("Worker execution 已被领取或队列状态不匹配")
        execution_id = executions[0]["execution_id"]
        stamp = now_shanghai()
        reason = args.reason or "Operator 人工撤回正式 AI 队列。"
        execution_changed = database.execute(
            "UPDATE executions SET status='FINISHED', finished_at=?, outcome=NULL, "
            "termination_reason=? WHERE execution_id=? AND task_id=? AND status='QUEUED'",
            (stamp, reason, execution_id, args.task_id),
        ).rowcount
        task_changed = database.execute(
            "UPDATE tasks SET status='PENDING', updated_at=?, "
            "progress_summary='正式 AI 排队已人工撤回。', "
            "progress_next_step='等待 Dispatcher 再次排队。', row_version=row_version+1 "
            "WHERE id=? AND status='QUEUED' AND preflight_status='READY' AND row_version=?",
            (stamp, args.task_id, task["row_version"]),
        ).rowcount
        if execution_changed != 1 or task_changed != 1:
            raise LoopError("撤回正式 AI 队列时任务发生并发变化")
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
            "VALUES(?, ?, 'QUEUED', 'PENDING', 'task-manager', ?)",
            (args.task_id, stamp, reason),
        )
        revision = bump_revision(database, "task-manager")
        commit(database)
        output(
            {
                "outcome": "EXECUTION_UNSCHEDULED",
                "task_id": args.task_id,
                "execution_id": execution_id,
                "status": "PENDING",
                "preflight_status": "READY",
                "row_version": int(task["row_version"]) + 1,
                "revision": revision,
            }
        )
    except Exception:
        rollback(database)
        raise
    finally:
        database.close()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Queue one formal Worker execution")
    root.add_argument("--config", default=str(CONFIG_PATH))
    root.add_argument("--db", default=str(BASE_DIR / "data" / "loop-agent.sqlite3"))
    return root


def main() -> None:
    command_schedule_execution(parser().parse_args())


if __name__ == "__main__":
    try:
        main()
    except (ExecutionDispatchError, sqlite3.Error) as error:
        print(json.dumps({"outcome": "EXECUTION_QUEUE_ERROR", "error": type(error).__name__}, ensure_ascii=False))
        raise SystemExit(1)
