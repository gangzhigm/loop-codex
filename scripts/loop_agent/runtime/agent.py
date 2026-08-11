"""Single-task orchestration for the self-hosted Agent runtime.

``SingleTaskAgent`` owns the sequence from one atomic claim to one final report.
It never claims a second task. The class coordinates heartbeat fencing, context
construction, provider requests, tool calls, bounded retries, one optional
final-schema repair, and the final ``loopctl finish`` call.

Retry policy is deliberately conservative: only trusted transient failures are
retryable, and a retry is suppressed after any local write. A provider thread
that remains alive after an attempt deadline raises ``OwnedWorkStillRunning``;
normal recovery must then quarantine its scope rather than start another writer.
"""

from __future__ import annotations

# 中文排查：SingleTaskAgent 串联一次 claim、上下文构建、模型循环、工具调用、重试和 finish。
# 失败时先定位 agent attempt，再看 model step；发生本地写入后不得重放完整 attempt。
# 若超时线程仍存活，必须保留隔离并等待恢复流程，不能启动第二个写入者。

import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from loop_agent.runtime.contracts import FINAL_RESULT_SCHEMA, HIGH_RISK_ACTIONS
from loop_agent.runtime.controller import HeartbeatGuard
from loop_agent.runtime.core import (
    AgentAttemptTimeout,
    AgentRuntimeError,
    ApprovalRequired,
    ExecutionProfile,
    ModelProvider,
    ModelRequestTimeout,
    OwnedWorkStillRunning,
    RuntimeSettings,
    SafeLogger,
    ToolRejected,
    safe_subprocess_environment,
)
from loop_agent.runtime.diagnostics import ProviderDiagnostic, TrustedDiagnosticError
from loop_agent.runtime.protocol import approved_actions, validate_model_response
from loop_agent.runtime.sandbox import ScopePolicy, ToolSandbox
from loopdb import (
    CAPABILITY_LEVELS,
    CANONICAL_RUNTIME_ENVIRONMENTS,
    load_initialization_config,
)


class SingleTaskAgent:
    """Run one claimed task to a fenced final control-plane report."""

    def __init__(
        self,
        provider: ModelProvider,
        controller: Any,
        workspace: Path,
        settings: RuntimeSettings,
        logger: SafeLogger | None = None,
        *,
        config: dict[str, Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider = provider
        self.controller = controller
        self.workspace = workspace.resolve()
        self.settings = settings
        self.logger = logger or SafeLogger()
        self.config = config or load_initialization_config()
        self.clock = clock

    def run(
        self,
        execution_id: str,
        runtime_environment: str,
        capability_level: str,
        provider_id: str | None = None,
    ) -> dict[str, Any]:
        """Claim once, run the task, and finish once."""
        if (
            runtime_environment not in CANONICAL_RUNTIME_ENVIRONMENTS
            or capability_level not in CAPABILITY_LEVELS
        ):
            raise AgentRuntimeError(
                "runtime environment and capability level must be explicit and valid"
            )
        execution_profile = ExecutionProfile.resolve(
            self.config, runtime_environment, provider_id, capability_level
        )
        validate_startup = getattr(self.provider, "validate_startup", None)
        if callable(validate_startup):
            validate_startup(execution_profile)
        claim = self.controller.claim(
            execution_id,
            execution_profile.runtime_environment,
            execution_profile.capability_level,
            execution_profile.provider_id,
        )
        outcome = claim.get("outcome")
        if outcome != "CLAIMED":
            if outcome not in {"NO_TASK", "SLOT_FULL", "CONFLICT"}:
                raise AgentRuntimeError("claim returned an unknown outcome")
            self.logger.event("claim_finished", outcome=outcome)
            return claim
        task = claim.get("task")
        if not isinstance(task, dict):
            raise AgentRuntimeError("claim omitted task")

        # Defense in depth: a compromised or stale controller response cannot
        # route a task to a different runtime, Provider, or capability level.
        expected_route = {
            "runtime_environment": execution_profile.runtime_environment,
            "provider_id": execution_profile.provider_id,
            "capability_level": execution_profile.capability_level,
        }
        if any(task.get(key) != value for key, value in expected_route.items()):
            result = self._failed(
                "claimed task routing does not match this runtime"
            )
            finish = self.controller.finish(
                execution_id, str(task.get("id") or ""), result
            )
            return {"outcome": "FINISHED", "result": result, "finish": finish}
        task_id = str(task.get("id") or "")
        try:
            policy = ScopePolicy(self.workspace, list(task.get("scope") or []))
            context = self._task_context(task, policy)
            approvals = approved_actions(task)
        except Exception as error:
            result = self._waiting(
                "任务上下文无法安全建立，需要人工检查 scope 或项目目录。",
                type(error).__name__,
            )
            finish = self.controller.finish(execution_id, task_id, result)
            return {"outcome": "FINISHED", "result": result, "finish": finish}

        def heartbeat() -> Any:
            return self.controller.heartbeat(execution_id, task_id)

        result: dict[str, Any]
        try:
            with HeartbeatGuard(
                heartbeat,
                self.settings.heartbeat_interval_seconds,
                self.logger,
            ) as guard:
                result = self._run_attempts(
                    context,
                    policy,
                    approvals,
                    guard,
                    execution_profile,
                    "credential_access" in approvals,
                )
                guard.ensure_healthy()
                guard.beat()
        except KeyboardInterrupt:
            result = self._failed("agent process was interrupted")
        except Exception as error:
            self.logger.event("agent_failed", error=type(error).__name__)
            result = self._failed(self._public_error(error))
        finish = self.controller.finish(execution_id, task_id, result)
        if finish.get("outcome") != "FINISHED":
            raise AgentRuntimeError("finish did not confirm task update")
        return {
            "outcome": "FINISHED",
            "task_id": task_id,
            "result": result,
            "finish": finish,
        }

    def _run_attempts(
        self,
        context: dict[str, Any],
        policy: ScopePolicy,
        approvals: set[str],
        guard: HeartbeatGuard,
        execution_profile: ExecutionProfile,
        credential_access_approved: bool,
    ) -> dict[str, Any]:
        """Run bounded attempts without replaying a local side effect."""
        maximum_attempts = execution_profile.max_retries + 1
        last_result = self._failed("agent attempt did not start")
        for attempt in range(1, maximum_attempts + 1):
            guard.ensure_healthy()
            guard.beat()
            sandbox = ToolSandbox(policy, self.settings, approvals)
            deadline = self.clock() + execution_profile.attempt_timeout_seconds
            self.logger.event(
                "agent_attempt_started",
                attempt=attempt,
                maximum_attempts=maximum_attempts,
                capability_level=execution_profile.capability_level,
            )
            attempt_error: Exception | None = None
            try:
                last_result = self._model_loop(
                    context,
                    sandbox,
                    guard,
                    credential_access_approved,
                    execution_profile,
                    deadline,
                )
            except OwnedWorkStillRunning:
                raise
            except Exception as error:
                attempt_error = error
                guard.ensure_healthy()
                diagnostic = self._trusted_diagnostic(error, agent_attempt=attempt)
                if getattr(error, "requires_human", False):
                    return self._waiting(
                        "Provider 配置、权限或凭据需要人工处理。",
                        self._public_error(error),
                        diagnostic,
                    )
                event_fields: dict[str, Any] = {
                    "attempt": attempt,
                    "error": type(error).__name__,
                }
                if diagnostic is not None:
                    event_fields.update(diagnostic.as_dict())
                self.logger.event("agent_attempt_failed", **event_fields)
                last_result = self._failed(self._public_error(error), diagnostic)
            if last_result.get("status") != "FAILED":
                return last_result
            transient_failure = self._is_retryable_attempt_failure(attempt_error)
            retryable = transient_failure and sandbox.side_effect_count == 0
            if not retryable or attempt >= maximum_attempts:
                if (
                    transient_failure
                    and sandbox.side_effect_count
                    and attempt < maximum_attempts
                ):
                    last_result = self._failed(
                        str(last_result.get("error") or "agent attempt failed")
                        + "; execution retry suppressed after a local side effect"
                    )
                elif attempt_error is not None and attempt < maximum_attempts:
                    self.logger.event(
                        "agent_attempt_not_retried",
                        attempt=attempt,
                        error=type(attempt_error).__name__,
                        reason="deterministic_or_unclassified_failure",
                    )
                return last_result
            guard.ensure_healthy()
            guard.beat()
            self.logger.event(
                "agent_attempt_retry", attempt=attempt, next_attempt=attempt + 1
            )
        return last_result

    def _task_context(
        self, task: dict[str, Any], policy: ScopePolicy
    ) -> dict[str, Any]:
        """Build the bounded context sent to the Provider."""
        instructions = []
        for path in policy.context_files():
            if path.stat().st_size > self.settings.max_file_bytes:
                raise AgentRuntimeError("AGENTS.md exceeds configured read limit")
            instructions.append(
                {
                    "path": path.relative_to(self.workspace).as_posix(),
                    "content": path.read_text(encoding="utf-8"),
                }
            )
        repositories = []
        seen: set[Path] = set()
        for root, directory_scope in policy.scope_roots:
            cursor = root if directory_scope else root.parent
            while cursor.is_relative_to(self.workspace):
                if (cursor / ".git").exists():
                    if cursor not in seen:
                        repositories.append(self._git_snapshot(cursor))
                        seen.add(cursor)
                    break
                if cursor == self.workspace:
                    break
                cursor = cursor.parent
        return {
            "task": {
                "id": task.get("id"),
                "description": task.get("description") or "",
                "scope": list(task.get("scope") or []),
                "acceptance": list(task.get("acceptance") or []),
                "dependencies": [
                    {"id": dependency, "state": "satisfied_before_claim"}
                    for dependency in task.get("depends_on") or []
                ],
            },
            "instructions": instructions,
            "existing_worktree": repositories,
            "constraints": {
                "encoding": "UTF-8",
                "timezone": "Asia/Shanghai",
                "preserve_existing_changes": True,
                "one_claim_one_task": True,
                "high_risk_actions": sorted(HIGH_RISK_ACTIONS),
            },
        }

    def _git_snapshot(self, repository: Path) -> dict[str, Any]:
        """Read a bounded, credential-free worktree snapshot for context."""
        environment = safe_subprocess_environment()
        environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_PAGER": "cat",
            }
        )
        git_name = shutil.which("git", path=os.environ.get("PATH", ""))
        if not git_name or Path(git_name).resolve().is_relative_to(self.workspace):
            return {
                "root": repository.relative_to(self.workspace).as_posix(),
                "status_short": "unavailable",
            }
        completed = subprocess.run(
            [
                str(Path(git_name).resolve()),
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "--no-pager",
                "status",
                "--short",
                "--ignore-submodules=all",
            ],
            cwd=repository,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=self.settings.tool_timeout_seconds,
            check=False,
        )
        status = (
            completed.stdout[: self.settings.max_tool_output_chars]
            if completed.returncode == 0
            else "unavailable"
        )
        return {
            "root": repository.relative_to(self.workspace).as_posix(),
            "status_short": status,
        }

    def _model_loop(
        self,
        context: dict[str, Any],
        sandbox: ToolSandbox,
        guard: HeartbeatGuard,
        credential_access_approved: bool,
        execution_profile: ExecutionProfile,
        deadline: float,
    ) -> dict[str, Any]:
        """Alternate Provider requests and sandboxed tool responses."""
        messages: list[dict[str, Any]] = [
            {"role": "runtime", "content": context}
        ]
        for step in range(1, self.settings.max_steps + 1):
            self._remaining_attempt_seconds(deadline)
            guard.ensure_healthy()
            request = {
                "protocol_version": "1.0",
                "step": step,
                "messages": messages,
                "tools": ToolSandbox.TOOL_SCHEMAS,
                "credential_access_approved": credential_access_approved,
                "execution_profile": execution_profile.request_payload(),
                "final_result_schema": FINAL_RESULT_SCHEMA,
            }
            self.logger.event("model_request", step=step)
            try:
                raw = self._call_provider(request, deadline)
                response = validate_model_response(raw)
            except Exception as error:
                if self._can_repair_final(error):
                    try:
                        response = self._repair_final(
                            request,
                            messages,
                            error,
                            step,
                            deadline,
                            guard,
                        )
                    except Exception as repair_error:
                        error = repair_error
                    else:
                        self.logger.event(
                            "model_response", step=step, type=response["type"]
                        )
                        return response["result"]
                diagnostic = self._trusted_diagnostic(error, model_step=step)
                if isinstance(error, TrustedDiagnosticError) and diagnostic is not None:
                    error.diagnostic = diagnostic
                event_fields: dict[str, Any] = {
                    "step": step,
                    "error": type(error).__name__,
                }
                if diagnostic is not None:
                    event_fields.update(diagnostic.as_dict())
                self.logger.event("model_request_failed", **event_fields)
                raise error
            self.logger.event("model_response", step=step, type=response["type"])
            if response["type"] == "final":
                return response["result"]
            tool_results = []
            for call in response["calls"]:
                self._remaining_attempt_seconds(deadline)
                guard.beat()
                self.logger.event("tool_call", step=step, tool=call["name"])
                try:
                    tool_output = sandbox.execute(
                        call["name"], call["arguments"]
                    )
                    tool_results.append(
                        {"id": call["id"], "ok": True, "output": tool_output}
                    )
                except ApprovalRequired as error:
                    return self._waiting(
                        f"高风险动作 {error.action} 缺少当前任务的明确批准。",
                        f"请确认是否批准 {error.action}。",
                    )
                except ToolRejected as error:
                    tool_results.append(
                        {
                            "id": call["id"],
                            "ok": False,
                            "error": str(error)[:500],
                        }
                    )
                guard.beat()
                self._remaining_attempt_seconds(deadline)
            messages.append(
                {"role": "provider", "content": {"tool_calls": response["calls"]}}
            )
            messages.append(
                {"role": "runtime", "content": {"tool_results": tool_results}}
            )
        return self._failed(
            "maximum agent steps exhausted before a valid final result"
        )

    def _can_repair_final(self, error: Exception) -> bool:
        return (
            self.settings.max_final_repairs == 1
            and isinstance(error, TrustedDiagnosticError)
            and error.diagnostic.category in {"final_schema", "invalid_final_json"}
        )

    def _repair_final(
        self,
        request: dict[str, Any],
        messages: list[dict[str, Any]],
        original_error: Exception,
        step: int,
        deadline: float,
        guard: HeartbeatGuard,
    ) -> dict[str, Any]:
        """Allow one tool-free correction after a malformed final response."""
        diagnostic = self._trusted_diagnostic(original_error, model_step=step)
        assert diagnostic is not None
        guard.ensure_healthy()
        guard.beat()
        self.logger.event(
            "model_final_repair_started",
            step=step,
            repair_attempt=1,
            category=diagnostic.category,
        )
        repair_request = {
            **request,
            "messages": [
                *messages,
                {
                    "role": "runtime",
                    "content": {
                        "final_repair": {
                            "reason": diagnostic.category,
                            "instruction": (
                                "Return one corrected final JSON object matching "
                                "final_result_schema. Do not call tools."
                            ),
                        }
                    },
                },
            ],
            "tools": [],
            "final_repair": True,
        }
        try:
            raw = self._call_provider(repair_request, deadline)
            response = validate_model_response(raw)
            if response["type"] != "final":
                raise TrustedDiagnosticError(
                    ProviderDiagnostic(
                        "final_schema", finish_reason="tool_calls"
                    )
                )
        except Exception as error:
            repair_diagnostic = self._trusted_diagnostic(error, model_step=step)
            fields: dict[str, Any] = {
                "step": step,
                "repair_attempt": 1,
                "error": type(error).__name__,
            }
            if repair_diagnostic is not None:
                fields.update(repair_diagnostic.as_dict())
                if isinstance(error, TrustedDiagnosticError):
                    error.diagnostic = repair_diagnostic
            self.logger.event("model_final_repair_failed", **fields)
            raise
        guard.beat()
        self.logger.event(
            "model_final_repair_succeeded", step=step, repair_attempt=1
        )
        return response

    def _call_provider(
        self, request: dict[str, Any], deadline: float
    ) -> dict[str, Any]:
        """Run one Provider request within the remaining attempt deadline."""
        completed: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
        attempt_remaining = self._remaining_attempt_seconds(deadline)
        request_timeout_seconds = min(
            self.settings.model_timeout_seconds, attempt_remaining
        )

        def invoke() -> None:
            try:
                completed.put(
                    (
                        "result",
                        self.provider.complete(request, request_timeout_seconds),
                    )
                )
            except BaseException as error:
                completed.put(("error", error))

        thread = threading.Thread(
            target=invoke, name="model-provider", daemon=True
        )
        thread.start()
        try:
            outcome, value = completed.get(timeout=attempt_remaining)
        except queue.Empty as error:
            thread.join(timeout=self.settings.provider_termination_grace_seconds)
            if thread.is_alive():
                raise OwnedWorkStillRunning(
                    "model provider remained active after the attempt timeout"
                ) from error
            raise AgentAttemptTimeout("agent attempt timed out") from error
        if outcome == "error":
            if isinstance(value, TimeoutError):
                raise ModelRequestTimeout(
                    "model provider request timed out"
                ) from value
            raise value
        self._remaining_attempt_seconds(deadline)
        return value

    def _remaining_attempt_seconds(self, deadline: float) -> float:
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise AgentAttemptTimeout("agent attempt timed out")
        return remaining

    @staticmethod
    def _failed(
        error: str, diagnostic: ProviderDiagnostic | None = None
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "FAILED",
            "summary": "自建 Agent 本轮执行失败。",
            "error": error[:4000],
        }
        if diagnostic is not None:
            result["diagnostic"] = diagnostic.as_dict()
        return result

    @staticmethod
    def _waiting(
        summary: str,
        question: str,
        diagnostic: ProviderDiagnostic | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "WAITING_HUMAN",
            "summary": summary[:4000],
            "question": question[:4000],
            "options": ["批准后重新排队", "保持等待"],
            "next_step": "等待人工决定后重新排队。",
        }
        if diagnostic is not None:
            result["diagnostic"] = diagnostic.as_dict()
        return result

    @staticmethod
    def _trusted_diagnostic(
        error: Exception,
        *,
        agent_attempt: int | None = None,
        model_step: int | None = None,
    ) -> ProviderDiagnostic | None:
        if isinstance(error, TrustedDiagnosticError):
            return error.diagnostic.with_context(
                agent_attempt=agent_attempt, model_step=model_step
            )
        return None

    @staticmethod
    def _is_retryable_attempt_failure(error: Exception | None) -> bool:
        if isinstance(error, (AgentAttemptTimeout, ModelRequestTimeout)):
            return True
        if isinstance(error, TrustedDiagnosticError):
            diagnostic = error.diagnostic
            return diagnostic.retryable and diagnostic.retry_exhausted
        return False

    @staticmethod
    def _public_error(error: Exception) -> str:
        """Return a bounded, credential-free failure description."""
        if isinstance(error, TrustedDiagnosticError):
            return error.diagnostic.public_text()
        if isinstance(error, AgentRuntimeError):
            return str(error)[:1000]
        return f"runtime error: {type(error).__name__}"
