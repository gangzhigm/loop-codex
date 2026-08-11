from __future__ import annotations

# 中文排查：Dispatcher 只选择一个可执行能力等级，并最多启动一次 Codex CLI Runner。
# 没有启动 Runner 时依次检查任务投影、依赖、平台容量、配置路径和 runner_path。
# 最终能否原子领取仍由 Runner/loopctl 决定，本文件不能自行锁定或修改任务。

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

SCRIPTS_ROOT = Path(__file__).resolve().parents[2]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

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
    resolve_execution_profile,
    state_payload,
)


RUNTIME_ENVIRONMENT = "codex_cli"
TERMINAL_RUNNER_OUTCOMES = {"NO_TASK", "SLOT_FULL", "CONFLICT"}


class DispatcherError(RuntimeError):
    pass


@dataclass(frozen=True)
class DispatcherSettings:
    config_path: Path
    database_path: Path
    runner_path: Path
    working_directory: Path
    interval_minutes: int
    task_name: str
    run_as_current_user: bool
    timeout_seconds: float
    log_path: Path
    hidden: bool
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
    ) -> "DispatcherSettings":
        cli = config.get("codex_cli")
        execution = config.get("task_execution")
        database = config.get("database")
        workspace = config.get("workspace")
        if not all(isinstance(value, dict) for value in (cli, execution, database, workspace)):
            raise DispatcherError("dispatcher configuration is incomplete")
        raw = cli.get("dispatcher")
        if not isinstance(raw, dict):
            raise DispatcherError("codex_cli.dispatcher configuration is missing")
        interval = raw.get("interval_minutes")
        task_name = raw.get("task_name")
        working_value = raw.get("working_directory")
        run_as_current_user = raw.get("run_as_current_user")
        timeout = raw.get("timeout_seconds")
        log_value = raw.get("log_path")
        hidden = raw.get("hidden")
        supported = raw.get("supported_capability_levels")
        if not isinstance(interval, int) or interval < 1:
            raise DispatcherError("dispatcher interval_minutes is invalid")
        if not isinstance(task_name, str) or not task_name.strip():
            raise DispatcherError("dispatcher task_name is invalid")
        if not isinstance(working_value, str) or not working_value.strip():
            raise DispatcherError("dispatcher working_directory is invalid")
        if not isinstance(run_as_current_user, bool) or not isinstance(hidden, bool):
            raise DispatcherError("dispatcher boolean settings are invalid")
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise DispatcherError("dispatcher timeout_seconds is invalid")
        if not isinstance(log_value, str) or not log_value.strip():
            raise DispatcherError("dispatcher log_path is invalid")
        if not isinstance(supported, list) or not supported or len(supported) != len(set(supported)):
            raise DispatcherError("dispatcher supported_capability_levels is invalid")
        if any(level not in CAPABILITY_LEVELS for level in supported):
            raise DispatcherError("dispatcher contains an unsupported capability level")
        enabled = ((config.get("execution_profiles") or {}).get(RUNTIME_ENVIRONMENT) or {}).get(
            "capabilities"
        )
        if not isinstance(enabled, dict) or not set(supported).issubset(enabled):
            raise DispatcherError("dispatcher capability levels must be configured for codex_cli")
        profiles = [
            resolve_execution_profile(RUNTIME_ENVIRONMENT, None, level, config)
            for level in supported
        ]
        termination_grace = cli.get("termination_grace_seconds")
        if not isinstance(termination_grace, (int, float)) or termination_grace <= 0:
            raise DispatcherError("dispatcher Codex CLI termination grace is invalid")
        required_timeout = max(
            profile["attempt_timeout_seconds"] * (profile["max_retries"] + 1)
            + termination_grace * (profile["max_retries"] + 1)
            for profile in profiles
        )
        if float(timeout) <= required_timeout:
            raise DispatcherError("dispatcher timeout must cover every complete execution attempt")
        root = base_dir.resolve()
        expected_working = Path(working_value).resolve()
        if expected_working != root:
            raise DispatcherError("dispatcher working_directory must be the Local Agent Loop root")
        database_path = (root / str(database.get("path") or "")).resolve()
        log_path = (root / log_value).resolve()
        runner_path = (
            root / "scripts" / "roles" / "runner" / "codex_cli_runner.py"
        ).resolve()
        if not database_path.is_relative_to(root) or not log_path.is_relative_to(root) or not runner_path.is_file():
            raise DispatcherError("dispatcher paths are unsafe or unavailable")
        maximum = execution.get("global_max_active_executions")
        limits = execution.get("platform_max_active_executions")
        if not isinstance(maximum, int) or maximum < 1 or not isinstance(limits, dict):
            raise DispatcherError("dispatcher execution limits are invalid")
        platform_maximum = limits.get(RUNTIME_ENVIRONMENT)
        if not isinstance(platform_maximum, int) or platform_maximum < 1:
            raise DispatcherError("dispatcher platform limit is invalid")
        if maximum != global_parallel_limit(config) or platform_maximum != platform_parallel_limit(
            RUNTIME_ENVIRONMENT, config
        ):
            raise DispatcherError("dispatcher execution limits are inconsistent")
        return cls(
            config_path=config_path.resolve(),
            database_path=database_path,
            runner_path=runner_path,
            working_directory=root,
            interval_minutes=interval,
            task_name=task_name.strip(),
            run_as_current_user=run_as_current_user,
            timeout_seconds=float(timeout),
            log_path=log_path,
            hidden=hidden,
            supported_capability_levels=tuple(supported),
            global_max_active_executions=maximum,
            platform_max_active_executions=platform_maximum,
        )


class EventLogger:
    """Write only dispatcher metadata, never child-process output."""

    def __init__(self, path: Path | None) -> None:
        self.path = path

    def event(self, outcome: str, **fields: object) -> None:
        payload = {"at": now_shanghai(), "outcome": outcome, **fields}
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line + "\n")


def dependencies_complete(task: dict[str, Any], statuses: dict[str, str]) -> bool:
    dependencies = task.get("depends_on")
    return isinstance(dependencies, list) and all(
        statuses.get(str(dependency)) in DEPENDENCY_COMPLETE_STATUSES for dependency in dependencies
    )


def select_candidate(
    tasks: list[dict[str, Any]], supported_capability_levels: tuple[str, ...]
) -> dict[str, Any] | None:
    statuses = {str(task.get("id")): str(task.get("status")) for task in tasks}
    for task in tasks:
        if (
            task.get("status") == "PENDING"
            and task.get("runtime_environment") == RUNTIME_ENVIRONMENT
            and task.get("provider_id") is None
            and task.get("capability_level") in supported_capability_levels
            and task.get("execution_policy") == "automatic"
            and dependencies_complete(task, statuses)
        ):
            return task
    return None


def default_snapshot(settings: DispatcherSettings, config: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    database = connect(settings.database_path)
    try:
        return all_tasks(database), state_payload(database, config)["agents"]
    finally:
        database.close()


def default_launcher(command: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=timeout)


class CodexCliDispatcher:
    def __init__(
        self,
        settings: DispatcherSettings,
        config: dict[str, Any],
        *,
        snapshot_reader: Callable[[DispatcherSettings, dict[str, Any]], tuple[list[dict[str, Any]], list[dict[str, Any]]]] = default_snapshot,
        launcher: Callable[[list[str], Path, float], subprocess.CompletedProcess[str]] = default_launcher,
        logger: EventLogger | None = None,
    ) -> None:
        self.settings = settings
        self.config = config
        self.snapshot_reader = snapshot_reader
        self.launcher = launcher
        self.logger = logger or EventLogger(settings.log_path)

    def run(self) -> dict[str, Any]:
        tasks, active_agents = self.snapshot_reader(self.settings, self.config)
        candidate = select_candidate(tasks, self.settings.supported_capability_levels)
        if candidate is None:
            self.logger.event("NO_TASK")
            return {"outcome": "NO_TASK"}
        task_id = str(candidate["id"])
        capability_level = str(candidate["capability_level"])
        if len(active_agents) >= self.settings.global_max_active_executions:
            self.logger.event(
                "SLOT_FULL", task_id=task_id, capability_level=capability_level,
                limit_scope="global",
            )
            return {
                "outcome": "SLOT_FULL", "task_id": task_id, "capability_level": capability_level,
                "limit_scope": "global",
            }
        active_for_platform = sum(
            agent.get("runtime_environment") == RUNTIME_ENVIRONMENT for agent in active_agents
        )
        if active_for_platform >= self.settings.platform_max_active_executions:
            self.logger.event(
                "SLOT_FULL", task_id=task_id, capability_level=capability_level,
                limit_scope="platform",
            )
            return {
                "outcome": "SLOT_FULL", "task_id": task_id, "capability_level": capability_level,
                "limit_scope": "platform",
            }
        command = [
            sys.executable,
            str(self.settings.runner_path),
            "--capability-level",
            capability_level,
            "--config",
            str(self.settings.config_path),
            "--db",
            str(self.settings.database_path),
        ]
        self.logger.event(
            "RUNNER_STARTED", task_id=task_id, capability_level=capability_level
        )
        try:
            completed = self.launcher(command, self.settings.working_directory, self.settings.timeout_seconds)
        except subprocess.TimeoutExpired:
            self.logger.event(
                "RUNNER_TIMEOUT", task_id=task_id, capability_level=capability_level
            )
            return {
                "outcome": "RUNNER_TIMEOUT", "task_id": task_id,
                "capability_level": capability_level,
            }
        except OSError as error:
            self.logger.event(
                "RUNNER_START_FAILED", task_id=task_id, capability_level=capability_level,
                error_type=type(error).__name__,
            )
            return {
                "outcome": "RUNNER_START_FAILED", "task_id": task_id,
                "capability_level": capability_level,
            }
        outcome = "RUNNER_FINISHED" if completed.returncode == 0 else "RUNNER_FAILED"
        self.logger.event(
            outcome, task_id=task_id, capability_level=capability_level,
            exit_code=completed.returncode,
        )
        return {
            "outcome": outcome, "task_id": task_id, "capability_level": capability_level,
            "exit_code": completed.returncode,
        }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Single-dispatch Codex CLI Runner launcher")
    root.add_argument("--config", default=str(CONFIG_PATH))
    root.add_argument("--db")
    root.add_argument("--dry-run", action="store_true")
    return root


def main() -> None:
    args = parser().parse_args()
    config_path = Path(args.config).resolve()
    config = load_initialization_config(config_path)
    settings = DispatcherSettings.from_config(config, config_path=config_path)
    if args.db:
        database_path = Path(args.db).resolve()
        if not database_path.is_relative_to(BASE_DIR):
            raise DispatcherError("dispatcher database path must remain in the Local Agent Loop root")
        settings = DispatcherSettings(**{**settings.__dict__, "database_path": database_path})
    if args.dry_run:
        print(json.dumps({"outcome": "DRY_RUN", "task_name": settings.task_name, "interval_minutes": settings.interval_minutes}, ensure_ascii=False))
        return
    print(json.dumps(CodexCliDispatcher(settings, config).run(), ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except DispatcherError as error:
        print(json.dumps({"outcome": "DISPATCHER_ERROR", "error": type(error).__name__}, ensure_ascii=False))
        raise SystemExit(1)
