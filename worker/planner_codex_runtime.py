"""Runner 启动的只读 Codex CLI Planner Worker。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = REPOSITORY_ROOT / "control"
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from loop_agent.runtime.controller import HeartbeatGuard
from loop_agent.runtime.core import ExecutionProfile, SafeLogger
from loopdb import CONFIG_PATH, load_initialization_config
from worker.codex_cli_runtime import (
    AUTH_ERROR,
    BoundedText,
    CodexCliRunner,
    CodexCliRunnerError,
    CodexCliSettings,
    sanitize_public_text,
)
from worker.planner_agent_runtime import (
    PlannerController,
    PlannerRunnerError,
    planner_result_schema,
    validate_planner_result,
)


class PlannerCodexRuntimeError(RuntimeError):
    """Planner Codex 进程、协议或受控写回不符合契约。"""


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Single-preflight Codex CLI Planner Worker")
    value.add_argument("--execution-id", required=True)
    value.add_argument("--config", default=str(CONFIG_PATH))
    value.add_argument("--db")
    return value


def _parse_result(output: str) -> tuple[str, dict[str, Any]]:
    candidates: list[str] = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "item.completed":
            item = event.get("item")
            if (
                isinstance(item, dict)
                and item.get("type") == "agent_message"
                and isinstance(item.get("text"), str)
            ):
                candidates.append(item["text"])
        elif event.get("type") == "agent_message" and isinstance(event.get("text"), str):
            candidates.append(event["text"])
    for candidate in reversed(candidates):
        try:
            return validate_planner_result(json.loads(candidate))
        except (json.JSONDecodeError, PlannerRunnerError):
            continue
    raise PlannerCodexRuntimeError("Codex CLI produced no valid Planner result")


def _prompt(config: dict[str, Any], task: dict[str, Any], attempt: int) -> str:
    authority = (REPOSITORY_ROOT / config["prompts"]["planner"]).read_text(encoding="utf-8")
    payload = {
        "execution_kind": "PLANNER",
        "attempt": attempt,
        "task": task,
        "host_contract": {
            "read_only": True,
            "direct_database_access_forbidden": True,
            "direct_loopctl_forbidden": True,
            "runner_owns_heartbeat_and_writeback": True,
        },
    }
    return f"{authority.rstrip()}\n\n# 当前任务\n\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n"


def _planner_codex_command(
    settings: CodexCliSettings,
    profile: ExecutionProfile,
    workspace: Path,
    schema_path: Path,
) -> list[str]:
    """构建只读 Planner Worker 的 Codex CLI 命令。"""
    return [
        *settings.command_prefix,
        "exec",
        "--json",
        "--ephemeral",
        *([] if settings.use_user_config else ["--ignore-user-config"]),
        "--ignore-rules",
        "--skip-git-repo-check",
        "--color",
        "never",
        "--model",
        profile.model,
        "--sandbox",
        "read-only",
        "--cd",
        str(workspace),
        "--output-schema",
        str(schema_path),
        "-c",
        f'model_reasoning_effort="{profile.reasoning}"',
        "-",
    ]


def _execute_attempt(
    helper: CodexCliRunner,
    settings: CodexCliSettings,
    profile: ExecutionProfile,
    config: dict[str, Any],
    task: dict[str, Any],
    attempt: int,
    guard: HeartbeatGuard,
) -> tuple[str, dict[str, Any]]:
    stdout = BoundedText(settings.max_stdout_chars)
    stderr = BoundedText(settings.max_stderr_chars)
    workspace = Path(config["workspace"]["task_root"]).resolve()
    with tempfile.TemporaryDirectory(prefix="local-agent-loop-planner-") as temporary:
        schema_path = Path(temporary) / "planner-result.schema.json"
        schema_path.write_text(
            json.dumps(planner_result_schema(), ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
        command = _planner_codex_command(settings, profile, workspace, schema_path)
        process = helper._start_process(command)
        helper._process = process
        readers = [
            helper._reader(process.stdout, stdout, "planner-stdout"),
            helper._reader(process.stderr, stderr, "planner-stderr"),
        ]
        assert process.stdin is not None
        try:
            process.stdin.write(_prompt(config, task, attempt))
            process.stdin.close()
            deadline = time.monotonic() + profile.attempt_timeout_seconds
            while process.poll() is None:
                guard.ensure_healthy()
                if time.monotonic() >= deadline:
                    raise PlannerCodexRuntimeError("Planner Codex CLI attempt timed out")
                time.sleep(settings.process_poll_interval_seconds)
        finally:
            if process.poll() is None:
                helper._terminate_process_tree(process)
            for reader in readers:
                reader.join(timeout=settings.termination_grace_seconds)
            helper._process = None
        if process.returncode != 0:
            diagnostic = sanitize_public_text(stderr.value())
            if AUTH_ERROR.search(diagnostic):
                raise PlannerCodexRuntimeError("Codex CLI login, account, or model access is unavailable")
            raise PlannerCodexRuntimeError(
                f"Planner Codex CLI exited with code {process.returncode}: {diagnostic}"
            )
        return _parse_result(stdout.value())


def run_planner_codex(args: argparse.Namespace) -> dict[str, Any]:
    config = load_initialization_config(Path(args.config))
    planner = config["planner"]
    runtime_environment = str(planner["worker_runtime_environment"])
    provider_id = planner.get("worker_provider_id")
    capability_level = str(planner["capability_level"])
    if runtime_environment != "codex_cli" or provider_id is not None:
        raise PlannerCodexRuntimeError("Planner Worker route is not codex_cli")
    profile = ExecutionProfile.resolve(
        config, runtime_environment, provider_id, capability_level
    )
    base_settings = CodexCliSettings.from_config(config)
    settings = replace(
        base_settings,
        prompt_path=(REPOSITORY_ROOT / config["prompts"]["planner"]).resolve(),
        sandbox="read-only",
        use_user_config=bool(config["codex_cli"]["planner_use_user_config"]),
        heartbeat_interval_seconds=float(planner["heartbeat_interval_seconds"]),
        stalled_after_seconds=float(planner["stalled_after_seconds"]),
    )
    controller = PlannerController(
        Path(args.db) if args.db else None,
        timeout_seconds=float(planner["controller_timeout_seconds"]),
    )
    claim = controller.claim(args.execution_id, runtime_environment)
    if claim.get("outcome") != "CLAIMED":
        return claim
    task = claim.get("task")
    if not isinstance(task, dict):
        raise PlannerCodexRuntimeError("Planner claim omitted task")
    task_id = str(task.get("id") or "")
    helper = CodexCliRunner(controller, config, settings, logger=SafeLogger())

    def heartbeat() -> dict[str, Any]:
        return controller.heartbeat(args.execution_id, task_id)

    last_error = "Planner Codex CLI attempt did not start"
    try:
        with HeartbeatGuard(heartbeat, settings.heartbeat_interval_seconds, SafeLogger()) as guard:
            for attempt in range(1, int(planner["max_retries"]) + 2):
                try:
                    outcome, report = _execute_attempt(
                        helper, settings, profile, config, task, attempt, guard
                    )
                    guard.ensure_healthy()
                    guard.beat()
                    finish = controller.finish(args.execution_id, task_id, outcome, report)
                    return {
                        "outcome": "FINISHED",
                        "planner_outcome": outcome,
                        "task_id": task_id,
                        "finish": finish,
                    }
                except Exception as error:
                    last_error = sanitize_public_text(str(error)) or type(error).__name__
                    if attempt >= int(planner["max_retries"]) + 1:
                        break
    except Exception as error:
        last_error = sanitize_public_text(str(error)) or type(error).__name__
    report = {
        "summary": "Planner Codex CLI 无法完成本轮只读预检。",
        "error": last_error,
        "evidence": ["Runner 已保留 Planner Codex CLI 的受限故障分类。"],
    }
    finish = controller.finish(args.execution_id, task_id, "FAILED", report)
    return {
        "outcome": "FINISHED",
        "planner_outcome": "FAILED",
        "task_id": task_id,
        "finish": finish,
    }


def main() -> None:
    args = parser().parse_args()
    try:
        print(json.dumps(run_planner_codex(args), ensure_ascii=False, indent=2))
    except Exception as error:
        print(
            json.dumps(
                {
                    "outcome": "PLANNER_WORKER_ERROR",
                    "error": sanitize_public_text(str(error)) or type(error).__name__,
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
