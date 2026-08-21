"""一次领取、一次内部 AI 静态预检、一次受控写回的 Planner Runner。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = REPOSITORY_ROOT / "control"
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from common.providers import call_provider, load_provider, provider_factory
from common.runners import RunnerState
from common.security import sanitize_public_text
from loop_agent.runtime.controller import HeartbeatGuard
from loop_agent.runtime.core import (
    ExecutionProfile,
    ModelProvider,
    RuntimeSettings,
    SafeLogger,
    safe_subprocess_environment,
)
from loop_agent.runtime.protocol import validate_tool_arguments
from loop_agent.runtime.sandbox import ScopePolicy, ToolSandbox
from loopdb import CAPABILITY_LEVELS, CONFIG_PATH, load_initialization_config


LOCK_MODES = {"file", "module", "project"}
CLAIM_TERMINAL_OUTCOMES = {"NO_TASK", "SLOT_FULL"}
PLANNER_TOOL_NAMES = {"read_file", "search", "run_command"}
PLANNER_TOOL_SCHEMAS = [
    item for item in ToolSandbox.TOOL_SCHEMAS if item["name"] in PLANNER_TOOL_NAMES
]


class PlannerRunnerError(RuntimeError):
    """Planner Runner 的配置、Provider 或结果协议不满足约束。"""


@dataclass(frozen=True)
class PlannerSettings:
    """冻结一次内部 AI 预检使用的路由、边界和超时。"""

    prompt_path: Path
    workspace: Path
    runtime_environment: str
    provider_id: str
    provider_specification: str
    capability_level: str
    heartbeat_interval_seconds: float
    attempt_timeout_seconds: float
    max_retries: int
    model_timeout_seconds: float
    provider_termination_grace_seconds: float
    max_steps: int

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "PlannerSettings":
        """从已校验配置解析 Planner 的内部 Provider 入口。"""
        planner = config["planner"]
        boundary = planner["client_boundary"]
        agent = config["self_hosted_agent"]
        workspace = Path(config["workspace"]["task_root"]).resolve()
        prompt_path = (REPOSITORY_ROOT / config["prompts"]["planner"]).resolve()
        if not prompt_path.is_file() or not prompt_path.is_relative_to(REPOSITORY_ROOT):
            raise PlannerRunnerError("Planner prompt path is unavailable")
        if boundary["sandbox"] != "read-only" or boundary["network_access"] is not False:
            raise PlannerRunnerError("Planner boundary must remain read-only and offline")
        runtime_environment = str(planner["default_runtime_environment"])
        provider_id = str(planner["provider_id"])
        capability_level = str(planner["capability_level"])
        if runtime_environment != "self_hosted_agent":
            raise PlannerRunnerError("Planner must use the self-hosted runtime")
        ExecutionProfile.resolve(config, runtime_environment, provider_id, capability_level)
        return cls(
            prompt_path=prompt_path,
            workspace=workspace,
            runtime_environment=runtime_environment,
            provider_id=provider_id,
            provider_specification=provider_factory(config, provider_id),
            capability_level=capability_level,
            heartbeat_interval_seconds=float(planner["heartbeat_interval_seconds"]),
            attempt_timeout_seconds=float(planner["attempt_timeout_seconds"]),
            max_retries=int(planner["max_retries"]),
            model_timeout_seconds=float(agent["model_timeout_seconds"]),
            provider_termination_grace_seconds=float(
                agent["provider_termination_grace_seconds"]
            ),
            max_steps=int(agent["max_steps"]),
        )


class PlannerController:
    """通过 loopctl 维护 Planner fencing、heartbeat 和最终写回。"""

    def __init__(
        self,
        database: Path | None,
        state: RunnerState | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        self.database = database.resolve() if database is not None else None
        self.state = state
        self.row_version: int | None = None
        self.timeout_seconds = timeout_seconds

    def _invoke(
        self, arguments: list[str], report: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        command = [sys.executable, str(CONTROL_ROOT / "loopctl.py")]
        if self.database is not None:
            command.extend(["--db", str(self.database)])
        command.extend(arguments)
        completed = subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            input=None if report is None else json.dumps(report, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=safe_subprocess_environment(),
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise PlannerRunnerError(f"Planner controller failed ({completed.returncode})")
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise PlannerRunnerError("Planner controller returned invalid JSON") from error
        if not isinstance(value, dict):
            raise PlannerRunnerError("Planner controller returned a non-object")
        return value

    def claim(self, execution_id: str, runtime_environment: str) -> dict[str, Any]:
        """原子预留一个匹配当前内部环境的草稿。"""
        result = self._invoke(
            [
                "preflight-claim",
                execution_id,
                "--runtime-environment",
                runtime_environment,
                "--sandbox",
                "read-only",
            ]
        )
        task = result.get("task")
        if result.get("outcome") == "CLAIMED" and isinstance(task, dict):
            self.row_version = int(task["row_version"])
            if self.state is not None:
                self.state.touch()
        elif self.state is not None:
            self.state.touch()
        return result

    def heartbeat(self, execution_id: str, task_id: str) -> dict[str, Any]:
        """续期预检并保存最终写回需要的最新 row_version。"""
        arguments = ["preflight-heartbeat", execution_id, task_id]
        if self.row_version is not None:
            arguments.extend(["--expected-row-version", str(self.row_version)])
        result = self._invoke(arguments)
        self.row_version = int(result["row_version"])
        if self.state is not None:
            self.state.touch()
        return result

    def finish(
        self,
        execution_id: str,
        task_id: str,
        outcome: str,
        report: dict[str, Any],
    ) -> dict[str, Any]:
        """把 Planner 结果映射到唯一允许的控制面命令。"""
        command = {
            "READY": "preflight-ready",
            "SPLIT": "preflight-split",
            "NEEDS_REVIEW": "preflight-needs-review",
            "FAILED": "preflight-fail",
        }.get(outcome)
        if command is None:
            raise PlannerRunnerError("Planner result outcome is invalid")
        arguments = [command, execution_id, task_id, "-"]
        if self.row_version is not None:
            arguments.extend(["--expected-row-version", str(self.row_version)])
        value = self._invoke(arguments, report)
        if self.state is not None:
            self.state.update(status="FINISHING", worker_pid=None)
        return value


class PlannerReadOnlySandbox:
    """在完整工作区内提供读取工具，并在执行层拒绝任何写入工具。"""

    def __init__(self, workspace: Path, runtime_settings: RuntimeSettings) -> None:
        self.sandbox = ToolSandbox(
            ScopePolicy(workspace, ["."]), runtime_settings, approved_actions=set()
        )

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in PLANNER_TOOL_NAMES:
            raise PlannerRunnerError("Planner requested a non-read-only tool")
        validate_tool_arguments(name, arguments)
        return self.sandbox.execute(name, arguments)


def planner_result_schema() -> dict[str, Any]:
    """声明内部 Provider 最终预检结果的统一外壳。"""
    string_array = {"type": "array", "items": {"type": "string"}}
    proposed_task = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "acceptance": string_array,
            "scope": string_array,
            "capability_level": {"type": "string", "enum": list(CAPABILITY_LEVELS)},
            "depends_on": string_array,
            "parallel_with": string_array,
        },
        "required": [
            "id",
            "title",
            "description",
            "acceptance",
            "scope",
            "capability_level",
            "depends_on",
            "parallel_with",
        ],
    }
    suggestion = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "reason": {"type": "string"},
            "tasks": {"type": "array", "items": proposed_task},
        },
        "required": ["reason", "tasks"],
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "outcome": {
                "type": "string",
                "enum": ["READY", "SPLIT", "NEEDS_REVIEW", "FAILED"],
            },
            "summary": {"type": "string"},
            "capability_level": {"type": ["string", "null"]},
            "scope": string_array,
            "lock_mode": {"type": ["string", "null"]},
            "technical_acceptance": string_array,
            "evidence": string_array,
            "question": {"type": ["string", "null"]},
            "options": string_array,
            "split_suggestions": {"type": "array", "items": suggestion},
            "error": {"type": ["string", "null"]},
        },
        "required": [
            "outcome",
            "summary",
            "capability_level",
            "scope",
            "lock_mode",
            "technical_acceptance",
            "evidence",
            "question",
            "options",
            "split_suggestions",
            "error",
        ],
    }


def _nonempty_strings(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, str) and bool(item.strip()) for item in value
    )


def validate_planner_result(value: Any) -> tuple[str, dict[str, Any]]:
    """把统一结果外壳收窄为控制面要求的三种精确报告。"""
    if not isinstance(value, dict):
        raise PlannerRunnerError("Planner final result must be an object")
    outcome = value.get("outcome")
    summary = value.get("summary")
    evidence = value.get("evidence")
    if not isinstance(summary, str) or not summary.strip() or not _nonempty_strings(evidence):
        raise PlannerRunnerError("Planner final result requires summary and evidence")
    if outcome == "READY":
        if value.get("capability_level") not in CAPABILITY_LEVELS:
            raise PlannerRunnerError("READY capability level is invalid")
        if value.get("lock_mode") not in LOCK_MODES:
            raise PlannerRunnerError("READY lock mode is invalid")
        if not _nonempty_strings(value.get("scope")) or not _nonempty_strings(
            value.get("technical_acceptance")
        ):
            raise PlannerRunnerError("READY scope and technical acceptance are required")
        return outcome, {
            "summary": summary,
            "capability_level": value["capability_level"],
            "scope": value["scope"],
            "lock_mode": value["lock_mode"],
            "technical_acceptance": value["technical_acceptance"],
            "evidence": evidence,
        }
    if outcome == "SPLIT":
        suggestions = value.get("split_suggestions")
        if not isinstance(suggestions, list) or len(suggestions) != 1:
            raise PlannerRunnerError("SPLIT requires exactly one split plan")
        return outcome, {
            "summary": summary,
            "split_suggestions": suggestions,
            "evidence": evidence,
        }
    if outcome == "NEEDS_REVIEW":
        question = value.get("question")
        options = value.get("options")
        suggestions = value.get("split_suggestions")
        if not isinstance(question, str) or not question.strip():
            raise PlannerRunnerError("NEEDS_REVIEW question is required")
        if not isinstance(options, list) or not all(isinstance(item, str) for item in options):
            raise PlannerRunnerError("NEEDS_REVIEW options are invalid")
        if not isinstance(suggestions, list):
            raise PlannerRunnerError("NEEDS_REVIEW split suggestions are invalid")
        return outcome, {
            "summary": summary,
            "question": question,
            "options": options,
            "split_suggestions": suggestions,
            "evidence": evidence,
        }
    if outcome == "FAILED":
        error = value.get("error")
        if not isinstance(error, str) or not error.strip():
            raise PlannerRunnerError("FAILED error is required")
        return outcome, {"summary": summary, "error": error, "evidence": evidence}
    raise PlannerRunnerError("Planner final result outcome is invalid")


class PlannerRunner:
    """编排一次 Planner claim、内部 Provider 工具循环和结果提交。"""

    def __init__(
        self,
        controller: PlannerController,
        settings: PlannerSettings,
        runtime_settings: RuntimeSettings,
        provider: ModelProvider,
        profile: ExecutionProfile,
        state: RunnerState,
        logger: SafeLogger | None = None,
    ) -> None:
        self.controller = controller
        self.settings = settings
        self.runtime_settings = runtime_settings
        self.provider = provider
        self.profile = profile
        self.state = state
        self.logger = logger or SafeLogger()

    def run(self, execution_id: str) -> dict[str, Any]:
        """领取一个草稿；有任务时完成预检并只提交一次结果。"""
        validate_startup = getattr(self.provider, "validate_startup", None)
        if callable(validate_startup):
            validate_startup(self.profile)
        claim = self.controller.claim(execution_id, self.settings.runtime_environment)
        outcome = claim.get("outcome")
        if outcome != "CLAIMED":
            if outcome not in CLAIM_TERMINAL_OUTCOMES:
                raise PlannerRunnerError("Planner claim returned an unknown outcome")
            return claim
        task = claim.get("task")
        boundary = claim.get("client_boundary")
        if not isinstance(task, dict) or not isinstance(boundary, dict):
            raise PlannerRunnerError("Planner claim omitted task or boundary")
        task_id = str(task.get("id") or "")
        operator_definition = task.get("operator_definition")
        route_matches = (
            isinstance(operator_definition, dict)
            and operator_definition.get("runtime_environment")
            == self.settings.runtime_environment
            and operator_definition.get("provider_id") == self.settings.provider_id
        )
        if not route_matches or not self._boundary_matches(claim, boundary):
            return self._fail(execution_id, task_id, "Planner claim boundary mismatch")

        def heartbeat() -> dict[str, Any]:
            return self.controller.heartbeat(execution_id, task_id)

        try:
            with HeartbeatGuard(
                heartbeat, self.settings.heartbeat_interval_seconds, self.logger
            ) as guard:
                planner_outcome, report = self._run_attempts(task, boundary, guard)
                guard.ensure_healthy()
                guard.beat()
            finish = self.controller.finish(
                execution_id, task_id, planner_outcome, report
            )
            return {
                "outcome": "FINISHED",
                "planner_outcome": planner_outcome,
                "task_id": task_id,
                "finish": finish,
            }
        except KeyboardInterrupt:
            return self._fail(execution_id, task_id, "Planner Runner was interrupted")
        except Exception as error:
            return self._fail(execution_id, task_id, self._public_error(error))

    @staticmethod
    def _boundary_matches(claim: dict[str, Any], boundary: dict[str, Any]) -> bool:
        return (
            claim.get("execution_kind") == "PLANNER"
            and boundary.get("sandbox") == "read-only"
            and boundary.get("approval_policy") == "never"
            and boundary.get("network_access") is False
            and boundary.get("default_tool_action") == "deny"
            and boundary.get("source_access") == "read-only"
            and boundary.get("writeback_transport")
            == "host_controlled_loopctl_stdin"
        )

    def _run_attempts(
        self,
        task: dict[str, Any],
        boundary: dict[str, Any],
        guard: HeartbeatGuard,
    ) -> tuple[str, dict[str, Any]]:
        """在总截止时间内重试无副作用的内部 Provider 预检。"""
        deadline = time.monotonic() + self.settings.attempt_timeout_seconds
        last_error = "Planner attempt did not start"
        for attempt in range(1, self.settings.max_retries + 2):
            guard.ensure_healthy()
            guard.beat()
            if time.monotonic() >= deadline:
                break
            try:
                return self._model_loop(task, boundary, guard, attempt, deadline)
            except Exception as error:
                last_error = self._public_error(error)
                self.logger.event(
                    "planner_attempt_failed",
                    attempt=attempt,
                    error=type(error).__name__,
                )
        raise PlannerRunnerError(last_error)

    def _model_loop(
        self,
        task: dict[str, Any],
        boundary: dict[str, Any],
        guard: HeartbeatGuard,
        attempt: int,
        deadline: float,
    ) -> tuple[str, dict[str, Any]]:
        """交替调用内部 Provider 和只读工具，直到得到合法预检结果。"""
        sandbox = PlannerReadOnlySandbox(self.settings.workspace, self.runtime_settings)
        messages: list[dict[str, Any]] = [
            {
                "role": "runtime",
                "content": self._context(task, boundary, attempt),
            }
        ]
        for step in range(1, self.settings.max_steps + 1):
            guard.ensure_healthy()
            guard.beat()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PlannerRunnerError("Planner attempt timed out")
            request = {
                "protocol_version": "1.0",
                "step": step,
                "messages": messages,
                "tools": PLANNER_TOOL_SCHEMAS,
                "credential_access_approved": True,
                "execution_profile": self.profile.request_payload(),
                "final_result_schema": planner_result_schema(),
            }
            raw = call_provider(
                self.provider,
                request,
                min(self.settings.model_timeout_seconds, remaining),
                remaining,
                self.settings.provider_termination_grace_seconds,
            )
            response_type = raw.get("type")
            if response_type == "final":
                return validate_planner_result(raw.get("result"))
            calls = raw.get("calls")
            if response_type != "tool_calls" or not isinstance(calls, list) or not calls:
                raise PlannerRunnerError("Planner Provider returned an invalid response")
            tool_results = []
            for call in calls:
                if not isinstance(call, dict):
                    raise PlannerRunnerError("Planner Provider returned an invalid tool call")
                call_id = call.get("id")
                name = call.get("name")
                arguments = call.get("arguments")
                if not isinstance(call_id, str) or not isinstance(name, str) or not isinstance(arguments, dict):
                    raise PlannerRunnerError("Planner Provider returned an invalid tool call")
                guard.beat()
                try:
                    output = sandbox.execute(name, arguments)
                    tool_results.append({"id": call_id, "ok": True, "output": output})
                except Exception as error:
                    tool_results.append(
                        {
                            "id": call_id,
                            "ok": False,
                            "error": self._public_error(error),
                        }
                    )
            messages.append(
                {"role": "provider", "content": {"tool_calls": calls}}
            )
            messages.append(
                {"role": "runtime", "content": {"tool_results": tool_results}}
            )
        raise PlannerRunnerError("Planner exhausted the configured model steps")

    def _context(
        self, task: dict[str, Any], boundary: dict[str, Any], attempt: int
    ) -> dict[str, Any]:
        """组合权威 Planner 规则和宿主已领取的最小任务载荷。"""
        return {
            "authority": self.settings.prompt_path.read_text(encoding="utf-8"),
            "execution_kind": "PLANNER",
            "runtime_environment": self.settings.runtime_environment,
            "provider_id": self.settings.provider_id,
            "attempt": attempt,
            "client_boundary": boundary,
            "task": task,
            "host_contract": {
                "runner_owns_heartbeat_and_writeback": True,
                "read_only_tools_only": True,
                "direct_loopctl_forbidden": True,
            },
        }

    def _fail(self, execution_id: str, task_id: str, error: str) -> dict[str, Any]:
        """已领取任务发生宿主故障时通过 preflight-fail 关闭预检。"""
        report = {
            "summary": "Planner Runner 无法完成本轮静态预检。",
            "error": sanitize_public_text(error, 4000),
            "evidence": ["Planner Runner 报告了可复现的内部 Provider 或宿主故障。"],
        }
        finish = self.controller.finish(execution_id, task_id, "FAILED", report)
        return {
            "outcome": "FINISHED",
            "planner_outcome": "FAILED",
            "task_id": task_id,
            "finish": finish,
        }

    @staticmethod
    def _public_error(error: Exception) -> str:
        if isinstance(error, PlannerRunnerError):
            return sanitize_public_text(str(error), 1000)
        return f"planner runner error: {type(error).__name__}"


def parser() -> argparse.ArgumentParser:
    """创建一次性 Planner Runner 命令行。"""
    value = argparse.ArgumentParser(description="Single-preflight internal Agent Runner")
    value.add_argument("--execution-id")
    value.add_argument("--config", default=str(CONFIG_PATH))
    value.add_argument("--db")
    return value


def main() -> None:
    """装配内部 Provider、状态和控制器后执行一次 Planner 预检。"""
    args = parser().parse_args()
    config = load_initialization_config(Path(args.config))
    settings = PlannerSettings.from_config(config)
    runtime_settings = RuntimeSettings.from_config(config)
    profile = ExecutionProfile.resolve(
        config,
        settings.runtime_environment,
        settings.provider_id,
        settings.capability_level,
    )
    provider = load_provider(settings.provider_specification, config)
    execution_id = args.execution_id or f"planner-{uuid.uuid4()}"
    state = RunnerState(
        execution_id, "planner", settings.runtime_environment, execution_id
    )
    state.start()
    try:
        controller = PlannerController(Path(args.db) if args.db else None, state)
        result = PlannerRunner(
            controller,
            settings,
            runtime_settings,
            provider,
            profile,
            state,
        ).run(execution_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        state.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            json.dumps(
                {
                    "outcome": "PLANNER_RUNNER_ERROR",
                    "error": PlannerRunner._public_error(error),
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(1)
