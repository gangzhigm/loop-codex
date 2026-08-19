"""Planner 内部的 Self-hosted Agent 单轮执行分发能力。

本模块只读任务与活动 execution 快照，判断是否需要启动一个 Runner。Runner 启动后
自行通过 loopctl 原子领取任务、维护 heartbeat、执行内部 Agent 并写回结果。
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = REPOSITORY_ROOT / "control"
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))

from common.processes import launch_detached_process
from common.providers import provider_factory
from loopdb import (
    BASE_DIR,
    CAPABILITY_LEVELS,
    CONFIG_PATH,
    DEPENDENCY_COMPLETE_STATUSES,
    all_tasks,
    connect,
    global_parallel_limit,
    load_initialization_config,
    now_shanghai,
    platform_parallel_limit,
    state_payload,
)


RUNTIME_ENVIRONMENT = "self_hosted_agent"


class ExecutionDispatchError(RuntimeError):
    """配置、路径或调度前置条件不满足内部 Agent 契约。"""


@dataclass(frozen=True)
class ExecutionDispatchSettings:
    """冻结 Planner 正式执行分发的路由、路径和并发限制。"""

    config_path: Path
    database_path: Path
    runner_path: Path
    working_directory: Path
    scheduled: bool
    interval_minutes: int
    log_path: Path
    provider_id: str
    provider_specification: str
    supported_capability_levels: tuple[str, ...]
    global_max_active_executions: int
    platform_max_active_executions: int

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        *,
        base_dir: Path = BASE_DIR,
        config_path: Path = CONFIG_PATH,
    ) -> "ExecutionDispatchSettings":
        """校验内部 Agent 路由和项目内路径后生成设置。"""
        planner = config.get("planner")
        raw = planner.get("execution_scheduler") if isinstance(planner, dict) else None
        execution = config.get("task_execution")
        database = config.get("database")
        if not all(isinstance(value, dict) for value in (raw, execution, database)):
            raise ExecutionDispatchError("planner execution scheduler configuration is incomplete")
        scheduled = raw.get("scheduled")
        interval = raw.get("interval_minutes")
        working_value = raw.get("working_directory")
        log_value = raw.get("log_path")
        runtime_environment = raw.get("runtime_environment")
        provider_id = raw.get("provider_id")
        supported = raw.get("supported_capability_levels")
        if not isinstance(scheduled, bool):
            raise ExecutionDispatchError("planner execution scheduler scheduled is invalid")
        if not isinstance(interval, int) or interval < 1:
            raise ExecutionDispatchError("planner execution scheduler interval_minutes is invalid")
        if runtime_environment != RUNTIME_ENVIRONMENT:
            raise ExecutionDispatchError("planner execution runtime must be self_hosted_agent")
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ExecutionDispatchError("planner execution provider_id is invalid")
        if not isinstance(working_value, str) or not working_value.strip():
            raise ExecutionDispatchError("planner execution working_directory is invalid")
        if not isinstance(log_value, str) or not log_value.strip():
            raise ExecutionDispatchError("planner execution log_path is invalid")
        if not isinstance(supported, list) or not supported or len(supported) != len(set(supported)):
            raise ExecutionDispatchError("planner execution capability levels are invalid")
        if any(level not in CAPABILITY_LEVELS for level in supported):
            raise ExecutionDispatchError("planner execution scheduler has an unsupported capability level")
        profiles = (
            ((config.get("execution_profiles") or {}).get(RUNTIME_ENVIRONMENT) or {})
            .get("providers", {})
            .get(provider_id, {})
            .get("capabilities")
        )
        if not isinstance(profiles, dict) or not set(supported).issubset(profiles):
            raise ExecutionDispatchError("planner execution capability levels lack provider profiles")
        root = base_dir.resolve()
        working_directory = Path(working_value).resolve()
        database_path = (root / str(database.get("path") or "")).resolve()
        log_path = (root / log_value).resolve()
        runner_path = (root / "runner" / "agent_runtime.py").resolve()
        if working_directory != root:
            raise ExecutionDispatchError("planner execution working_directory must be the project root")
        if (
            not database_path.is_relative_to(root)
            or not log_path.is_relative_to(root)
            or not runner_path.is_file()
        ):
            raise ExecutionDispatchError("planner execution paths are unsafe or unavailable")
        maximum = execution.get("global_max_active_executions")
        limits = execution.get("platform_max_active_executions")
        if not isinstance(maximum, int) or maximum < 1 or not isinstance(limits, dict):
            raise ExecutionDispatchError("planner execution limits are invalid")
        platform_maximum = limits.get(RUNTIME_ENVIRONMENT)
        if not isinstance(platform_maximum, int) or platform_maximum < 1:
            raise ExecutionDispatchError("planner execution platform limit is invalid")
        if maximum != global_parallel_limit(config) or platform_maximum != platform_parallel_limit(
            RUNTIME_ENVIRONMENT, config
        ):
            raise ExecutionDispatchError("planner execution limits are inconsistent")
        return cls(
            config_path=config_path.resolve(),
            database_path=database_path,
            runner_path=runner_path,
            working_directory=root,
            scheduled=scheduled,
            interval_minutes=interval,
            log_path=log_path,
            provider_id=provider_id,
            provider_specification=provider_factory(config, provider_id),
            supported_capability_levels=tuple(supported),
            global_max_active_executions=maximum,
            platform_max_active_executions=platform_maximum,
        )


class EventLogger:
    """只把调度结果元数据追加到 UTF-8 JSONL 日志。"""

    def __init__(self, path: Path | None) -> None:
        self.path = path

    def event(self, outcome: str, **fields: object) -> None:
        payload = {"at": now_shanghai(), "outcome": outcome, **fields}
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def dependencies_complete(task: dict[str, Any], statuses: dict[str, str]) -> bool:
    """确认候选任务的依赖都已进入允许继续执行的完成状态。"""
    dependencies = task.get("depends_on")
    return isinstance(dependencies, list) and all(
        statuses.get(str(dependency)) in DEPENDENCY_COMPLETE_STATUSES
        for dependency in dependencies
    )


def select_candidate(
    tasks: list[dict[str, Any]],
    provider_id: str,
    supported_capability_levels: tuple[str, ...],
) -> dict[str, Any] | None:
    """按数据库投影顺序选择首个匹配内部 Provider 的自动任务。"""
    statuses = {str(task.get("id")): str(task.get("status")) for task in tasks}
    for task in tasks:
        if (
            task.get("status") == "PENDING"
            and task.get("preflight_status") == "READY"
            and task.get("runtime_environment") == RUNTIME_ENVIRONMENT
            and task.get("provider_id") == provider_id
            and task.get("capability_level") in supported_capability_levels
            and task.get("execution_policy") == "automatic"
            and dependencies_complete(task, statuses)
        ):
            return task
    return None


def default_snapshot(
    settings: ExecutionDispatchSettings, config: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """在一个短数据库连接内读取任务和活动 execution 投影。"""
    database = connect(settings.database_path)
    try:
        return all_tasks(database), state_payload(database, config)["agents"]
    finally:
        database.close()


def default_launcher(command: list[str], cwd: Path) -> int:
    """启动独立 Runner，Planner 取得 PID 后立即放手。"""
    return launch_detached_process(command, cwd)


class ExecutionDispatcher:
    """完成一次候选选择、容量判断和内部 Agent Runner 启动。"""

    def __init__(
        self,
        settings: ExecutionDispatchSettings,
        config: dict[str, Any],
        *,
        snapshot_reader: Callable[
            [ExecutionDispatchSettings, dict[str, Any]],
            tuple[list[dict[str, Any]], list[dict[str, Any]]],
        ] = default_snapshot,
        launcher: Callable[[list[str], Path], int] = default_launcher,
        logger: EventLogger | None = None,
    ) -> None:
        self.settings = settings
        self.config = config
        self.snapshot_reader = snapshot_reader
        self.launcher = launcher
        self.logger = logger or EventLogger(settings.log_path)

    def run(self) -> dict[str, Any]:
        """最多启动一个 Runner，不等待其领取、执行或写回。"""
        tasks, active_agents = self.snapshot_reader(self.settings, self.config)
        candidate = select_candidate(
            tasks,
            self.settings.provider_id,
            self.settings.supported_capability_levels,
        )
        if candidate is None:
            self.logger.event("NO_TASK")
            return {"outcome": "NO_TASK"}
        task_id = str(candidate["id"])
        capability_level = str(candidate["capability_level"])
        if len(active_agents) >= self.settings.global_max_active_executions:
            return self._slot_full(task_id, capability_level, "global")
        active_for_platform = sum(
            agent.get("runtime_environment") == RUNTIME_ENVIRONMENT
            for agent in active_agents
        )
        if active_for_platform >= self.settings.platform_max_active_executions:
            return self._slot_full(task_id, capability_level, "platform")
        execution_id = f"agent-{capability_level.lower()}-{uuid.uuid4()}"
        command = [
            sys.executable,
            "-B",
            str(self.settings.runner_path),
            "--runtime-environment",
            RUNTIME_ENVIRONMENT,
            "--provider-id",
            self.settings.provider_id,
            "--capability-level",
            capability_level,
            "--execution-policy",
            "automatic",
            "--provider",
            self.settings.provider_specification,
            "--execution-id",
            execution_id,
            "--config",
            str(self.settings.config_path),
            "--db",
            str(self.settings.database_path),
        ]
        try:
            runner_pid = self.launcher(command, self.settings.working_directory)
        except OSError as error:
            self.logger.event(
                "RUNNER_START_FAILED",
                candidate_task_id=task_id,
                capability_level=capability_level,
                error_type=type(error).__name__,
            )
            return {
                "outcome": "RUNNER_START_FAILED",
                "candidate_task_id": task_id,
                "capability_level": capability_level,
            }
        self.logger.event(
            "RUNNER_STARTED",
            candidate_task_id=task_id,
            capability_level=capability_level,
            provider_id=self.settings.provider_id,
            runner_pid=runner_pid,
        )
        return {
            "outcome": "RUNNER_STARTED",
            "candidate_task_id": task_id,
            "capability_level": capability_level,
            "provider_id": self.settings.provider_id,
            "runner_pid": runner_pid,
        }

    def _slot_full(
        self, task_id: str, capability_level: str, limit_scope: str
    ) -> dict[str, Any]:
        """返回稳定容量结果，避免 Scheduler 把无空位误判为启动故障。"""
        self.logger.event(
            "SLOT_FULL",
            task_id=task_id,
            capability_level=capability_level,
            limit_scope=limit_scope,
        )
        return {
            "outcome": "SLOT_FULL",
            "task_id": task_id,
            "capability_level": capability_level,
            "limit_scope": limit_scope,
        }


def parser() -> argparse.ArgumentParser:
    """创建单轮内部 Agent 调度命令行。"""
    root = argparse.ArgumentParser(description="Planner single execution Runner dispatcher")
    root.add_argument("--config", default=str(CONFIG_PATH))
    root.add_argument("--db")
    root.add_argument("--dry-run", action="store_true")
    return root


def main() -> None:
    args = parser().parse_args()
    config_path = Path(args.config).resolve()
    config = load_initialization_config(config_path)
    settings = ExecutionDispatchSettings.from_config(config, config_path=config_path)
    if args.db:
        database_path = Path(args.db).resolve()
        if not database_path.is_relative_to(BASE_DIR):
            raise ExecutionDispatchError("planner execution database path must remain in the project root")
        settings = ExecutionDispatchSettings(**{**settings.__dict__, "database_path": database_path})
    if args.dry_run:
        print(json.dumps({"outcome": "DRY_RUN", "provider_id": settings.provider_id}, ensure_ascii=False))
        return
    print(json.dumps(ExecutionDispatcher(settings, config).run(), ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except ExecutionDispatchError as error:
        print(
            json.dumps(
                {"outcome": "EXECUTION_DISPATCH_ERROR", "error": type(error).__name__},
                ensure_ascii=False,
            )
        )
        raise SystemExit(1)
