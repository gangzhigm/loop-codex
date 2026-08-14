"""Codex CLI 平台的单次调度入口。

Dispatcher 读取任务和活动执行快照，只判断“当前是否值得启动一个 Runner”。它按
数据库既有顺序选择首个满足环境、能力等级、自动策略和依赖条件的 PENDING 任务，
再检查全局与平台并发容量。真正的原子领取、scope 冲突复核和 execution 创建仍由
Runner 调用 loopctl 完成，因此多个 Dispatcher 同时看到同一候选也不会获得双重
写权限。

每次运行最多启动一个 Runner。Runner 创建成功后立即返回，由 Runner 独立完成领取、
执行、写回和退出；日志只记录候选任务、能力等级和 Runner PID。
"""

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

CONTROL_ROOT = Path(__file__).resolve().parents[1] / "control"
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))

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


RUNTIME_ENVIRONMENT = "codex_cli"
_RUNNER_PROCESSES: set[subprocess.Popen[bytes]] = set()


class DispatcherError(RuntimeError):
    """配置、路径或调度前置条件不合法时抛出的公开异常。"""

    pass


@dataclass(frozen=True)
class DispatcherSettings:
    """从初始化配置冻结出的 Dispatcher 运行参数。

    路径在构造时解析为绝对路径，并限制在 Loop Agent 根目录内；并发上限必须与统一
    配置解析函数一致。Runner 的执行超时由 Runner 自己管理，不属于 Dispatcher 设置。
    """
    config_path: Path
    database_path: Path
    runner_path: Path
    working_directory: Path
    scheduled: bool
    interval_minutes: int
    log_path: Path
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
        """严格校验配置结构、能力映射和路径边界后生成设置。"""
        cli = config.get("codex_cli")
        execution = config.get("task_execution")
        database = config.get("database")
        if not all(isinstance(value, dict) for value in (cli, execution, database)):
            raise DispatcherError("dispatcher configuration is incomplete")
        raw = cli.get("dispatcher")
        if not isinstance(raw, dict):
            raise DispatcherError("codex_cli.dispatcher configuration is missing")
        scheduled = raw.get("scheduled")
        interval = raw.get("interval_minutes")
        working_value = raw.get("working_directory")
        log_value = raw.get("log_path")
        supported = raw.get("supported_capability_levels")
        if not isinstance(scheduled, bool):
            raise DispatcherError("dispatcher scheduled is invalid")
        if not isinstance(interval, int) or interval < 1:
            raise DispatcherError("dispatcher interval_minutes is invalid")
        if not isinstance(working_value, str) or not working_value.strip():
            raise DispatcherError("dispatcher working_directory is invalid")
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
        root = base_dir.resolve()
        expected_working = Path(working_value).resolve()
        if expected_working != root:
            raise DispatcherError("dispatcher working_directory must be the Local Agent Loop root")
        database_path = (root / str(database.get("path") or "")).resolve()
        log_path = (root / log_value).resolve()
        runner_path = (
            root / "runner" / "codex_cli_runner.py"
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
            scheduled=scheduled,
            interval_minutes=interval,
            log_path=log_path,
            supported_capability_levels=tuple(supported),
            global_max_active_executions=maximum,
            platform_max_active_executions=platform_maximum,
        )


class EventLogger:
    """以 UTF-8 JSONL 追加调度元数据，禁止写入子进程标准输出。"""

    def __init__(self, path: Path | None) -> None:
        """保存可选日志路径；传入 ``None`` 时完全禁用落盘。"""
        self.path = path

    def event(self, outcome: str, **fields: object) -> None:
        """追加一条带上海时区时间戳的紧凑事件记录。"""
        payload = {"at": now_shanghai(), "outcome": outcome, **fields}
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line + "\n")


def dependencies_complete(task: dict[str, Any], statuses: dict[str, str]) -> bool:
    """判断任务声明的所有依赖是否都已进入允许继续执行的完成状态。"""
    dependencies = task.get("depends_on")
    return isinstance(dependencies, list) and all(
        statuses.get(str(dependency)) in DEPENDENCY_COMPLETE_STATUSES for dependency in dependencies
    )


def select_candidate(
    tasks: list[dict[str, Any]], supported_capability_levels: tuple[str, ...]
) -> dict[str, Any] | None:
    """按快照顺序返回首个可交给 Codex CLI 的自动任务。

    本函数只做静态过滤，不检查容量和 scope 锁，也不修改数据库。最终能否领取必须
    以 Runner 的原子 claim 结果为准。
    """
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
    """在一次短连接中读取完整任务投影与当前活动执行列表。"""
    database = connect(settings.database_path)
    try:
        return all_tasks(database), state_payload(database, config)["agents"]
    finally:
        database.close()


def reap_runner_processes() -> None:
    """释放已经退出的 Runner 进程句柄，不读取或解释其执行结果。"""
    # 只保留尚未退出的 Popen 对象，避免常驻 Dispatcher 累积 Windows 进程句柄。
    for process in tuple(_RUNNER_PROCESSES):
        if process.poll() is not None:
            _RUNNER_PROCESSES.discard(process)


def default_launcher(command: list[str], cwd: Path) -> int:
    """创建与 Dispatcher 解耦的 Runner，成功后立即返回其 PID。"""
    reap_runner_processes()
    creation_flags = (
        subprocess.CREATE_NEW_PROCESS_GROUP
        | subprocess.DETACHED_PROCESS
        | subprocess.CREATE_NO_WINDOW
    )
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creation_flags,
    )
    _RUNNER_PROCESSES.add(process)
    return process.pid


class CodexCliDispatcher:
    """完成一次候选选择、容量判断、Runner 启动和元数据记录。"""

    def __init__(
        self,
        settings: DispatcherSettings,
        config: dict[str, Any],
        *,
        snapshot_reader: Callable[[DispatcherSettings, dict[str, Any]], tuple[list[dict[str, Any]], list[dict[str, Any]]]] = default_snapshot,
        launcher: Callable[[list[str], Path], int] = default_launcher,
        logger: EventLogger | None = None,
    ) -> None:
        """注入设置、快照读取器和启动器，便于测试时隔离数据库与真实进程。"""
        self.settings = settings
        self.config = config
        self.snapshot_reader = snapshot_reader
        self.launcher = launcher
        self.logger = logger or EventLogger(settings.log_path)

    def run(self) -> dict[str, Any]:
        """执行一次调度并返回稳定 outcome，不循环等待也不重试。

        无任务和容量已满属于正常结果。Runner 创建成功后立即返回，不等待执行结果，
        也不读取 Runner 的 stdout 或 stderr。
        """
        reap_runner_processes()
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
        try:
            runner_pid = self.launcher(command, self.settings.working_directory)
        except OSError as error:
            self.logger.event(
                "RUNNER_START_FAILED", candidate_task_id=task_id,
                capability_level=capability_level,
                error_type=type(error).__name__,
            )
            return {
                "outcome": "RUNNER_START_FAILED", "candidate_task_id": task_id,
                "capability_level": capability_level,
            }
        self.logger.event(
            "RUNNER_STARTED", candidate_task_id=task_id,
            capability_level=capability_level, runner_pid=runner_pid,
        )
        return {
            "outcome": "RUNNER_STARTED",
            "candidate_task_id": task_id,
            "capability_level": capability_level,
            "runner_pid": runner_pid,
        }


def parser() -> argparse.ArgumentParser:
    """构建单次调度命令行，支持覆盖配置、数据库和只校验不启动模式。"""
    root = argparse.ArgumentParser(description="Single-dispatch Codex CLI Runner launcher")
    root.add_argument("--config", default=str(CONFIG_PATH))
    root.add_argument("--db")
    root.add_argument("--dry-run", action="store_true")
    return root


def main() -> None:
    """加载初始化配置，应用受根目录约束的数据库覆盖，并执行一次调度。"""
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
        print(json.dumps({"outcome": "DRY_RUN", "interval_minutes": settings.interval_minutes}, ensure_ascii=False))
        return
    print(json.dumps(CodexCliDispatcher(settings, config).run(), ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except DispatcherError as error:
        print(json.dumps({"outcome": "DISPATCHER_ERROR", "error": type(error).__name__}, ensure_ascii=False))
        raise SystemExit(1)
