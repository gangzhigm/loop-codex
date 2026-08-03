from __future__ import annotations

import argparse
import importlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

sys.dont_write_bytecode = True

from loopdb import CONFIG_PATH, EXECUTION_PROFILES, RUNTIME_ENVIRONMENTS, load_initialization_config


BASE_DIR = Path(__file__).resolve().parent.parent
LOOPCTL = BASE_DIR / "scripts" / "loopctl.py"
FINAL_STATUSES = {"SUCCEEDED", "FAILED", "WAITING_HUMAN"}
HIGH_RISK_ACTIONS = {"delete", "publish", "git_commit", "external_message", "credential_access"}
SENSITIVE_COMPONENT = re.compile(
    r"(^|[._-])(secret|secrets|credential|credentials|api[_-]?key|access[_-]?token|private[_-]?key)([._-]|$)",
    re.IGNORECASE,
)
SHELL_META = re.compile(r"[|&;<>`\r\n]")


class AgentRuntimeError(RuntimeError):
    pass


class ToolRejected(AgentRuntimeError):
    pass


class ApprovalRequired(AgentRuntimeError):
    def __init__(self, action: str) -> None:
        self.action = action
        super().__init__(f"action requires explicit approval: {action}")


class ModelProvider(Protocol):
    """Provider boundary. Implementations translate a model API to this neutral contract."""

    def complete(self, request: dict[str, Any], timeout_seconds: float) -> dict[str, Any]: ...


@dataclass(frozen=True)
class RuntimeSettings:
    max_steps: int
    model_timeout_seconds: float
    tool_timeout_seconds: float
    heartbeat_interval_seconds: float
    max_file_bytes: int
    max_tool_output_chars: int

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "RuntimeSettings":
        agent = config["self_hosted_agent"]
        return cls(
            max_steps=int(agent["max_steps"]),
            model_timeout_seconds=float(agent["model_timeout_seconds"]),
            tool_timeout_seconds=float(agent["tool_timeout_seconds"]),
            heartbeat_interval_seconds=float(config["task_execution"]["heartbeat_interval_seconds"]),
            max_file_bytes=int(agent["max_file_bytes"]),
            max_tool_output_chars=int(agent["max_tool_output_chars"]),
        )


class SafeLogger:
    """Logs event metadata only; never request prompts, model reasoning, or file contents."""

    def __init__(self, stream: Any = None) -> None:
        self.stream = stream or sys.stderr

    def event(self, name: str, **fields: Any) -> None:
        safe = {key: str(value)[:160] for key, value in fields.items() if key not in {"content", "prompt", "authorization"}}
        print(json.dumps({"event": name, **safe}, ensure_ascii=False), file=self.stream, flush=True)


class SubprocessLoopController:
    def __init__(self, database: Path | None = None, timeout_seconds: float = 30) -> None:
        self.database = database
        self.timeout_seconds = timeout_seconds
        self.claim_count = 0

    def _invoke(self, arguments: list[str], input_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        command = [sys.executable, str(LOOPCTL)]
        if self.database is not None:
            command.extend(["--db", str(self.database.resolve())])
        command.extend(arguments)
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            command,
            cwd=BASE_DIR,
            input=None if input_payload is None else json.dumps(input_payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise AgentRuntimeError(f"loop controller failed ({completed.returncode})")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise AgentRuntimeError("loop controller returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise AgentRuntimeError("loop controller returned a non-object")
        return payload

    def claim(self, execution_id: str, runtime_environment: str, profile: str) -> dict[str, Any]:
        if self.claim_count:
            raise AgentRuntimeError("claim may only be called once per runtime instance")
        self.claim_count += 1
        return self._invoke(
            ["claim", execution_id, "--runtime-environment", runtime_environment, "--profile", profile]
        )

    def heartbeat(self, execution_id: str, task_id: str) -> dict[str, Any]:
        return self._invoke(["heartbeat", execution_id, task_id])

    def finish(self, execution_id: str, task_id: str, result: dict[str, Any]) -> dict[str, Any]:
        return self._invoke(["finish", execution_id, task_id, "-"], input_payload=result)


class HeartbeatGuard:
    def __init__(
        self,
        heartbeat: Callable[[], Any],
        interval_seconds: float,
        logger: SafeLogger,
    ) -> None:
        self.heartbeat = heartbeat
        self.interval_seconds = interval_seconds
        self.logger = logger
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.error: Exception | None = None

    def __enter__(self) -> "HeartbeatGuard":
        self.beat()
        self.thread = threading.Thread(target=self._run, name="agent-heartbeat", daemon=True)
        self.thread.start()
        return self

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval_seconds):
            try:
                self.beat()
            except Exception as error:  # pragma: no cover - race depends on wall clock
                self.error = error
                self.logger.event("heartbeat_failed", error=type(error).__name__)
                self.stop_event.set()

    def beat(self) -> None:
        self.heartbeat()
        self.logger.event("heartbeat")

    def ensure_healthy(self) -> None:
        if self.error is not None:
            raise AgentRuntimeError("heartbeat failed") from self.error

    def __exit__(self, *_: Any) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=max(1.0, self.interval_seconds))


class ScopePolicy:
    def __init__(self, workspace: Path, scopes: list[str]) -> None:
        self.workspace = workspace.resolve()
        if not self.workspace.is_dir():
            raise AgentRuntimeError("workspace directory does not exist")
        if not scopes:
            raise AgentRuntimeError("claimed task has no scope")
        self.scope_roots: list[tuple[Path, bool]] = []
        for scope in scopes:
            if not isinstance(scope, str) or not scope.strip():
                raise AgentRuntimeError("claimed task contains invalid scope")
            raw = Path(scope)
            if raw.is_absolute() or ".." in raw.parts or self._is_sensitive(raw.parts):
                raise AgentRuntimeError("claimed task contains unsafe scope")
            target = (self.workspace / raw).resolve()
            if not target.is_relative_to(self.workspace):
                raise AgentRuntimeError("claimed scope escapes workspace")
            directory_scope = scope.endswith(("/", "\\")) or target.is_dir()
            self.scope_roots.append((target, directory_scope))

    @staticmethod
    def _is_sensitive(parts: tuple[str, ...]) -> bool:
        for part in parts:
            lowered = part.lower()
            if (
                lowered in {
                    ".reasonix", "$codex_home", ".git", ".hg", ".svn", ".ssh", ".aws", ".azure", ".kube",
                    ".npmrc", ".pypirc", ".netrc", "id_rsa", "id_ed25519",
                }
                or lowered == ".env"
                or lowered.startswith(".env.")
            ):
                return True
            if lowered.endswith((".pem", ".p12", ".pfx", ".key")) or SENSITIVE_COMPONENT.search(part):
                return True
        return False

    def resolve(self, value: str, *, must_exist: bool = False, directory: bool | None = None) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise ToolRejected("path must be a non-empty relative path")
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ToolRejected("absolute and parent-relative paths are forbidden")
        if self._is_sensitive(candidate.parts):
            raise ToolRejected("sensitive paths are forbidden")
        resolved = (self.workspace / candidate).resolve()
        allowed = any(
            (directory_scope and (resolved == root or resolved.is_relative_to(root)))
            or (not directory_scope and resolved == root)
            for root, directory_scope in self.scope_roots
        )
        if not allowed:
            raise ToolRejected("path is outside the claimed scope")
        if self._is_sensitive(resolved.relative_to(self.workspace).parts):
            raise ToolRejected("sensitive paths are forbidden")
        if must_exist and not resolved.exists():
            raise ToolRejected("path does not exist")
        if directory is True and (not resolved.exists() or not resolved.is_dir()):
            raise ToolRejected("directory does not exist")
        if directory is False and resolved.exists() and not resolved.is_file():
            raise ToolRejected("path is not a file")
        return resolved

    def context_files(self) -> list[Path]:
        candidates: set[Path] = set()
        for root, directory_scope in self.scope_roots:
            cursor = root if directory_scope else root.parent
            while cursor.is_relative_to(self.workspace):
                agent_file = cursor / "AGENTS.md"
                if agent_file.is_file() and not self._is_sensitive(agent_file.parts):
                    candidates.add(agent_file)
                if cursor == self.workspace:
                    break
                cursor = cursor.parent
        return sorted(candidates, key=lambda item: (len(item.parts), str(item).lower()))


class ToolSandbox:
    TOOL_SCHEMAS = [
        {"name": "read_file", "arguments": {"path": "relative UTF-8 file path"}},
        {"name": "search", "arguments": {"path": "relative directory", "pattern": "regular expression"}},
        {"name": "apply_patch", "arguments": {"path": "relative file", "old": "exact text", "new": "replacement"}},
        {"name": "run_command", "arguments": {"argv": "safe argv array", "cwd": "relative directory"}},
    ]

    def __init__(
        self,
        policy: ScopePolicy,
        settings: RuntimeSettings,
        approved_actions: set[str],
    ) -> None:
        self.policy = policy
        self.settings = settings
        self.approved_actions = approved_actions

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name in HIGH_RISK_ACTIONS:
            if name not in self.approved_actions:
                raise ApprovalRequired(name)
            raise ToolRejected(f"high-risk action has no implementation: {name}")
        if not isinstance(arguments, dict):
            raise ToolRejected("tool arguments must be an object")
        handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "read_file": self._read_file,
            "search": self._search,
            "apply_patch": self._apply_patch,
            "run_command": self._run_command,
        }
        handler = handlers.get(name)
        if handler is None:
            raise ToolRejected("unknown tool")
        return handler(arguments)

    def _bounded(self, text: str) -> tuple[str, bool]:
        maximum = self.settings.max_tool_output_chars
        return (text[:maximum], len(text) > maximum)

    def _read_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self.policy.resolve(str(arguments.get("path", "")), must_exist=True, directory=False)
        if path.stat().st_size > self.settings.max_file_bytes:
            raise ToolRejected("file exceeds configured read limit")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise ToolRejected("file is not valid UTF-8 text") from error
        content, truncated = self._bounded(text)
        return {"path": path.relative_to(self.policy.workspace).as_posix(), "content": content, "truncated": truncated}

    def _search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        directory = self.policy.resolve(str(arguments.get("path", "")), must_exist=True, directory=True)
        pattern_text = arguments.get("pattern")
        if not isinstance(pattern_text, str) or not pattern_text or len(pattern_text) > 500:
            raise ToolRejected("search pattern is invalid")
        try:
            pattern = re.compile(pattern_text)
        except re.error as error:
            raise ToolRejected("search pattern is not a valid regular expression") from error
        matches: list[dict[str, Any]] = []
        for path in sorted(directory.rglob("*")):
            if len(matches) >= 200 or not path.is_file() or path.is_symlink():
                continue
            try:
                relative = path.relative_to(self.policy.workspace).as_posix()
                checked = self.policy.resolve(relative, must_exist=True, directory=False)
            except ToolRejected:
                continue
            if checked.stat().st_size > self.settings.max_file_bytes:
                continue
            try:
                lines = checked.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(lines, start=1):
                if pattern.search(line):
                    excerpt, _ = self._bounded(line)
                    matches.append({"path": relative, "line": number, "text": excerpt})
                    if len(matches) >= 200:
                        break
        return {"matches": matches, "truncated": len(matches) >= 200}

    def _apply_patch(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self.policy.resolve(str(arguments.get("path", "")), directory=False)
        old = arguments.get("old")
        new = arguments.get("new")
        if not isinstance(old, str) or not isinstance(new, str):
            raise ToolRejected("patch requires string old and new text")
        if not path.exists() and old == "":
            if len(new.encode("utf-8")) > self.settings.max_file_bytes:
                raise ToolRejected("new file exceeds configured edit limit")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new, encoding="utf-8", newline="")
            return {"path": path.relative_to(self.policy.workspace).as_posix(), "changed": True, "created": True}
        if not old:
            raise ToolRejected("empty old text is only valid when creating a missing file")
        if path.exists() and path.stat().st_size > self.settings.max_file_bytes:
            raise ToolRejected("file exceeds configured edit limit")
        try:
            text = path.read_text(encoding="utf-8") if path.exists() else ""
        except UnicodeDecodeError as error:
            raise ToolRejected("file is not valid UTF-8 text") from error
        occurrences = text.count(old)
        if occurrences != 1:
            raise ToolRejected(f"patch old text must match exactly once; matched {occurrences}")
        updated = text.replace(old, new, 1)
        if len(updated.encode("utf-8")) > self.settings.max_file_bytes:
            raise ToolRejected("patched file exceeds configured edit limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated, encoding="utf-8", newline="")
        return {"path": path.relative_to(self.policy.workspace).as_posix(), "changed": True}

    def _run_command(self, arguments: dict[str, Any]) -> dict[str, Any]:
        argv = arguments.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
            raise ToolRejected("argv must be a non-empty string array")
        if len(argv) > 32 or any(SHELL_META.search(item) or "\x00" in item for item in argv):
            raise ToolRejected("shell metacharacters and oversized commands are forbidden")
        cwd_value = arguments.get("cwd")
        cwd = self.policy.resolve(str(cwd_value or ""), must_exist=True, directory=True)
        executable = Path(argv[0]).name.lower()
        command: list[str]
        if executable in {"git", "git.exe"}:
            command = self._safe_git(argv)
        elif executable in {"rg", "rg.exe"}:
            command = self._safe_rg(argv, cwd)
        else:
            raise ToolRejected("command is not in the safe allowlist")
        command[0] = str(self._trusted_executable(command[0]))
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "SystemRoot": os.environ.get("SystemRoot", ""),
            "TEMP": os.environ.get("TEMP", ""),
            "TMP": os.environ.get("TMP", ""),
            "PYTHONIOENCODING": "utf-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_PAGER": "cat",
            "RIPGREP_CONFIG_PATH": os.devnull,
        }
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=self.settings.tool_timeout_seconds,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise ToolRejected("command timed out") from error
        stdout, stdout_truncated = self._bounded(completed.stdout)
        stderr, stderr_truncated = self._bounded(completed.stderr)
        return {
            "returncode": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": stdout_truncated or stderr_truncated,
        }

    @staticmethod
    def _safe_git(argv: list[str]) -> list[str]:
        if len(argv) < 2 or argv[1] not in {"status", "diff"}:
            raise ToolRejected("only git status and git diff are allowed")
        if argv[1] == "status":
            if argv[2:] not in ([], ["--short"]):
                raise ToolRejected("git status only accepts --short")
            return [
                "git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                "--no-pager", "status", "--short", "--ignore-submodules=all",
            ]
        allowed = {"--check", "--stat", "--name-only", "--name-status"}
        if any(item not in allowed for item in argv[2:]):
            raise ToolRejected("git diff arguments are restricted")
        return [
            "git", "-c", "core.fsmonitor=false", "-c", "diff.external=",
            "--no-pager", "diff", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all", *argv[2:]
        ]

    def _safe_rg(self, argv: list[str], cwd: Path) -> list[str]:
        if len(argv) < 2:
            raise ToolRejected("rg requires a pattern")
        forbidden = {
            "--pre", "--pre-glob", "--search-zip", "--follow", "-L", "--hostname-bin",
            "--hidden", "--no-ignore", "--no-ignore-vcs", "--unrestricted", "-u", "-uu", "-uuu",
        }
        if any(item in forbidden or item.startswith("--pre=") for item in argv[1:]):
            raise ToolRejected("rg option can execute or escape the sandbox")
        path_tokens = [item for item in argv[2:] if not item.startswith("-")]
        for token in path_tokens:
            candidate = Path(token)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise ToolRejected("rg path argument escapes the sandbox")
            target = (cwd / candidate).resolve()
            relative = target.relative_to(self.policy.workspace).as_posix()
            self.policy.resolve(relative, must_exist=True)
        return [
            "rg", "--no-config", "--color", "never", *argv[1:],
            "-g", "!.env*", "-g", "!**/.env*", "-g", "!**/.git/**", "-g", "!**/.reasonix/**",
            "-g", "!**/*secret*", "-g", "!**/*credential*", "-g", "!**/*private-key*",
        ]

    def _trusted_executable(self, name: str) -> Path:
        resolved_name = shutil.which(name, path=os.environ.get("PATH", ""))
        if not resolved_name:
            raise ToolRejected("allowed command is not installed")
        executable = Path(resolved_name).resolve()
        if executable.is_relative_to(self.policy.workspace):
            raise ToolRejected("command executable cannot come from the workspace")
        return executable


def approved_actions(task: dict[str, Any]) -> set[str]:
    text = "\n".join(
        [str(task.get("description") or ""), *[str(item) for item in task.get("acceptance") or []]]
    )
    match = re.search(r"APPROVED_ACTIONS\s*:\s*([a-z_, -]+)", text, re.IGNORECASE)
    if not match:
        return set()
    return {item.strip().lower() for item in match.group(1).split(",") if item.strip().lower() in HIGH_RISK_ACTIONS}


def validate_model_response(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("type") not in {"tool_calls", "final"}:
        raise AgentRuntimeError("model returned an invalid response envelope")
    if raw["type"] == "tool_calls":
        calls = raw.get("calls")
        if not isinstance(calls, list) or not calls or len(calls) > 8:
            raise AgentRuntimeError("model returned invalid tool calls")
        normalized = []
        known_tools = {item["name"] for item in ToolSandbox.TOOL_SCHEMAS} | HIGH_RISK_ACTIONS
        for call in calls:
            if not isinstance(call, dict) or not isinstance(call.get("id"), str) or not call["id"]:
                raise AgentRuntimeError("model returned a tool call without id")
            if not isinstance(call.get("name"), str) or not isinstance(call.get("arguments"), dict):
                raise AgentRuntimeError("model returned an invalid tool call")
            if call["name"] not in known_tools:
                raise AgentRuntimeError("model requested an unknown tool")
            validate_tool_arguments(call["name"], call["arguments"])
            normalized.append({"id": call["id"], "name": call["name"], "arguments": call["arguments"]})
        return {"type": "tool_calls", "calls": normalized}
    return {"type": "final", "result": validate_final_result(raw.get("result"))}


def validate_tool_arguments(name: str, arguments: dict[str, Any]) -> None:
    """Reject extra or malformed arguments before the sandbox sees a model tool call."""
    # High-risk names are deliberately forwarded to ToolSandbox so its approval gate can
    # produce WAITING_HUMAN; they still cannot reach an implementation without approval.
    if name in HIGH_RISK_ACTIONS:
        return
    schemas: dict[str, dict[str, Any]] = {
        "read_file": {"required": {"path": str}, "optional": {}},
        "search": {"required": {"path": str, "pattern": str}, "optional": {}},
        "apply_patch": {"required": {"path": str, "old": str, "new": str}, "optional": {}},
        "run_command": {"required": {"argv": list, "cwd": str}, "optional": {}},
    }
    schema = schemas.get(name)
    if schema is None:
        raise AgentRuntimeError("model requested an unknown tool")
    allowed = set(schema["required"]) | set(schema["optional"])
    if set(arguments) - allowed:
        raise AgentRuntimeError("model tool call contains unknown arguments")
    for field, expected in schema["required"].items():
        if field not in arguments or not isinstance(arguments[field], expected):
            raise AgentRuntimeError("model tool call has invalid arguments")
    if name == "run_command" and (
        not arguments["argv"] or not all(isinstance(item, str) and item for item in arguments["argv"])
    ):
        raise AgentRuntimeError("model tool call has invalid arguments")


def validate_final_result(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("status") not in FINAL_STATUSES:
        raise AgentRuntimeError("final result status is invalid")
    status = raw["status"]
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise AgentRuntimeError("final result summary is required")
    result: dict[str, Any] = {"status": status, "summary": summary.strip()[:4000]}
    verification = raw.get("verification") or []
    if not isinstance(verification, list) or not all(isinstance(item, str) and item.strip() for item in verification):
        raise AgentRuntimeError("verification must be a string array")
    if status == "SUCCEEDED" and not verification:
        raise AgentRuntimeError("SUCCEEDED requires verification")
    if verification:
        result["verification"] = [item.strip()[:1000] for item in verification[:50]]
    completed = raw.get("completed") or []
    if completed:
        if not isinstance(completed, list) or not all(isinstance(item, str) for item in completed):
            raise AgentRuntimeError("completed must be a string array")
        result["completed"] = [item.strip()[:1000] for item in completed[:50] if item.strip()]
    if status == "FAILED":
        error = raw.get("error")
        if not isinstance(error, str) or not error.strip():
            raise AgentRuntimeError("FAILED requires error")
        result["error"] = error.strip()[:4000]
    if status == "WAITING_HUMAN":
        question = raw.get("question")
        if not isinstance(question, str) or not question.strip():
            raise AgentRuntimeError("WAITING_HUMAN requires question")
        result["question"] = question.strip()[:4000]
        options = raw.get("options") or []
        if not isinstance(options, list) or not all(isinstance(item, str) for item in options):
            raise AgentRuntimeError("options must be a string array")
        result["options"] = [item.strip()[:500] for item in options[:20] if item.strip()]
        result["next_step"] = str(raw.get("next_step") or "等待人工答复。").strip()[:1000]
        result["percent"] = max(0, min(99, int(raw.get("percent", 0))))
    return result


class SingleTaskAgent:
    def __init__(
        self,
        provider: ModelProvider,
        controller: Any,
        workspace: Path,
        settings: RuntimeSettings,
        logger: SafeLogger | None = None,
    ) -> None:
        self.provider = provider
        self.controller = controller
        self.workspace = workspace.resolve()
        self.settings = settings
        self.logger = logger or SafeLogger()

    def run(self, execution_id: str, runtime_environment: str, profile: str) -> dict[str, Any]:
        if runtime_environment not in RUNTIME_ENVIRONMENTS or profile not in EXECUTION_PROFILES:
            raise AgentRuntimeError("runtime environment and execution profile must be explicit and valid")
        claim = self.controller.claim(execution_id, runtime_environment, profile)
        outcome = claim.get("outcome")
        if outcome != "CLAIMED":
            if outcome not in {"NO_TASK", "SLOT_FULL", "CONFLICT"}:
                raise AgentRuntimeError("claim returned an unknown outcome")
            self.logger.event("claim_finished", outcome=outcome)
            return claim
        task = claim.get("task")
        if not isinstance(task, dict):
            raise AgentRuntimeError("claim omitted task")
        if task.get("runtime_environment") != runtime_environment or task.get("execution_profile") != profile:
            result = self._failed("claimed task routing does not match this runtime")
            finish = self.controller.finish(execution_id, str(task.get("id") or ""), result)
            return {"outcome": "FINISHED", "result": result, "finish": finish}
        task_id = str(task.get("id") or "")
        try:
            policy = ScopePolicy(self.workspace, list(task.get("scope") or []))
            context = self._task_context(task, policy)
            approvals = approved_actions(task)
            sandbox = ToolSandbox(policy, self.settings, approvals)
        except Exception as error:
            result = self._waiting("任务上下文无法安全建立，需要人工检查 scope 或项目目录。", type(error).__name__)
            finish = self.controller.finish(execution_id, task_id, result)
            return {"outcome": "FINISHED", "result": result, "finish": finish}

        def heartbeat() -> Any:
            return self.controller.heartbeat(execution_id, task_id)

        result: dict[str, Any]
        try:
            with HeartbeatGuard(heartbeat, self.settings.heartbeat_interval_seconds, self.logger) as guard:
                result = self._model_loop(context, sandbox, guard, "credential_access" in approvals)
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
        return {"outcome": "FINISHED", "task_id": task_id, "result": result, "finish": finish}

    def _task_context(self, task: dict[str, Any], policy: ScopePolicy) -> dict[str, Any]:
        instructions = []
        for path in policy.context_files():
            if path.stat().st_size > self.settings.max_file_bytes:
                raise AgentRuntimeError("AGENTS.md exceeds configured read limit")
            instructions.append(
                {"path": path.relative_to(self.workspace).as_posix(), "content": path.read_text(encoding="utf-8")}
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
        environment = os.environ.copy()
        environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_PAGER": "cat"})
        git_name = shutil.which("git", path=os.environ.get("PATH", ""))
        if not git_name or Path(git_name).resolve().is_relative_to(self.workspace):
            return {"root": repository.relative_to(self.workspace).as_posix(), "status_short": "unavailable"}
        completed = subprocess.run(
            [
                str(Path(git_name).resolve()), "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                "--no-pager", "status", "--short", "--ignore-submodules=all",
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
        status = completed.stdout[: self.settings.max_tool_output_chars] if completed.returncode == 0 else "unavailable"
        return {"root": repository.relative_to(self.workspace).as_posix(), "status_short": status}

    def _model_loop(
        self,
        context: dict[str, Any],
        sandbox: ToolSandbox,
        guard: HeartbeatGuard,
        credential_access_approved: bool,
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [{"role": "runtime", "content": context}]
        for step in range(1, self.settings.max_steps + 1):
            guard.ensure_healthy()
            request = {
                "protocol_version": "1.0",
                "step": step,
                "messages": messages,
                "tools": ToolSandbox.TOOL_SCHEMAS,
                "credential_access_approved": credential_access_approved,
                "final_result_schema": {
                    "status": sorted(FINAL_STATUSES),
                    "required": {"SUCCEEDED": ["summary", "verification"], "FAILED": ["summary", "error"], "WAITING_HUMAN": ["summary", "question"]},
                },
            }
            self.logger.event("model_request", step=step)
            raw = self._call_provider(request)
            response = validate_model_response(raw)
            self.logger.event("model_response", step=step, type=response["type"])
            if response["type"] == "final":
                return response["result"]
            tool_results = []
            for call in response["calls"]:
                guard.beat()
                self.logger.event("tool_call", step=step, tool=call["name"])
                try:
                    output = sandbox.execute(call["name"], call["arguments"])
                    tool_results.append({"id": call["id"], "ok": True, "output": output})
                except ApprovalRequired as error:
                    return self._waiting(
                        f"高风险动作 {error.action} 缺少当前任务的明确批准。",
                        f"请确认是否批准 {error.action}。",
                    )
                except ToolRejected as error:
                    tool_results.append({"id": call["id"], "ok": False, "error": str(error)[:500]})
                guard.beat()
            messages.append({"role": "provider", "content": {"tool_calls": response["calls"]}})
            messages.append({"role": "runtime", "content": {"tool_results": tool_results}})
        return self._failed("maximum agent steps exhausted before a valid final result")

    def _call_provider(self, request: dict[str, Any]) -> dict[str, Any]:
        completed: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                completed.put(("result", self.provider.complete(request, self.settings.model_timeout_seconds)))
            except BaseException as error:
                completed.put(("error", error))

        thread = threading.Thread(target=invoke, name="model-provider", daemon=True)
        thread.start()
        try:
            outcome, value = completed.get(timeout=self.settings.model_timeout_seconds)
        except queue.Empty as error:
            raise AgentRuntimeError("model provider timed out") from error
        if outcome == "error":
            if isinstance(value, TimeoutError):
                raise AgentRuntimeError("model provider timed out") from value
            raise value
        return value

    @staticmethod
    def _failed(error: str) -> dict[str, Any]:
        return {"status": "FAILED", "summary": "自建 Agent 本轮执行失败。", "error": error[:4000]}

    @staticmethod
    def _waiting(summary: str, question: str) -> dict[str, Any]:
        return {
            "status": "WAITING_HUMAN",
            "summary": summary[:4000],
            "question": question[:4000],
            "options": ["批准后重新排队", "保持等待"],
            "next_step": "等待人工决定后重新排队。",
        }

    @staticmethod
    def _public_error(error: Exception) -> str:
        if isinstance(error, AgentRuntimeError):
            return str(error)[:1000]
        return f"runtime error: {type(error).__name__}"


def load_provider(specification: str) -> ModelProvider:
    if ":" not in specification:
        raise AgentRuntimeError("provider must use module:factory syntax")
    module_name, attribute = specification.split(":", 1)
    factory = getattr(importlib.import_module(module_name), attribute)
    provider = factory()
    if not callable(getattr(provider, "complete", None)):
        raise AgentRuntimeError("provider factory did not return a ModelProvider")
    return provider


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Single-run self-hosted Local Agent Loop runtime")
    root.add_argument("--runtime-environment", required=True, choices=RUNTIME_ENVIRONMENTS)
    root.add_argument("--profile", required=True, choices=EXECUTION_PROFILES)
    root.add_argument("--provider", required=True, help="Python module:factory returning a ModelProvider")
    root.add_argument("--execution-id", required=True)
    root.add_argument("--config", default=str(CONFIG_PATH))
    root.add_argument("--db")
    return root


def main() -> None:
    args = parser().parse_args()
    config = load_initialization_config(Path(args.config))
    workspace = Path(config["workspace"]["task_root"])
    provider = load_provider(args.provider)
    validate_startup = getattr(provider, "validate_startup", None)
    if callable(validate_startup):
        validate_startup(args.runtime_environment, args.profile)
    agent = SingleTaskAgent(
        provider=provider,
        controller=SubprocessLoopController(Path(args.db) if args.db else None),
        workspace=workspace,
        settings=RuntimeSettings.from_config(config),
    )
    result = agent.run(args.execution_id, args.runtime_environment, args.profile)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"outcome": "RUNTIME_ERROR", "error": SingleTaskAgent._public_error(error)}, ensure_ascii=False))
        raise SystemExit(1)
