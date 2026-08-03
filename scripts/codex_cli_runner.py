from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

sys.dont_write_bytecode = True

from agent_runtime import (
    AgentRuntimeError,
    HeartbeatGuard,
    SafeLogger,
    ScopePolicy,
    SubprocessLoopController,
    validate_final_result,
)
from loopdb import (
    CONFIG_PATH,
    EXECUTION_PROFILES,
    configured_projects,
    load_initialization_config,
    resolve_scope_key,
)


BASE_DIR = Path(__file__).resolve().parent.parent
RUNTIME_ENVIRONMENT = "codex_cli"
CLAIM_TERMINAL_OUTCOMES = {"NO_TASK", "SLOT_FULL", "CONFLICT"}
REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}
SAFE_SANDBOXES = {"read-only", "workspace-write"}
AUTH_ERROR = re.compile(
    r"(?i)(not logged in|login required|authentication|unauthori[sz]ed|forbidden|"
    r"account (?:is )?(?:unavailable|disabled)|model .{0,40}(?:access|permission)|\b(?:401|403)\b)"
)
SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|authorization)\s*[:=]\s*\S+"),
    re.compile(r"(?i)(?:[A-Za-z]:\\|/)[^\r\n\"']*[\\/](?:\.codex|\$CODEX_HOME)(?:[\\/][^\r\n\"']*)?"),
)


class CodexCliRunnerError(RuntimeError):
    pass


class CodexCliTimeout(CodexCliRunnerError):
    pass


@dataclass(frozen=True)
class ProfileSettings:
    model: str
    reasoning_effort: str


@dataclass(frozen=True)
class CodexCliSettings:
    command_prefix: tuple[str, ...]
    prompt_path: Path
    use_user_config: bool
    sandbox: str
    timeout_seconds: float
    termination_grace_seconds: float
    heartbeat_interval_seconds: float
    max_stdout_chars: int
    max_stderr_chars: int
    profiles: dict[str, ProfileSettings]

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        *,
        base_dir: Path = BASE_DIR,
        command_prefix: tuple[str, ...] | None = None,
    ) -> "CodexCliSettings":
        raw = config.get("codex_cli")
        if not isinstance(raw, dict):
            raise CodexCliRunnerError("codex_cli configuration is missing")
        executable = raw.get("executable")
        prompt_value = raw.get("prompt")
        supported = raw.get("supported_execution_profiles")
        profile_values = raw.get("profiles")
        if not isinstance(executable, str) or not executable.strip():
            raise CodexCliRunnerError("codex_cli executable is invalid")
        if not isinstance(prompt_value, str) or not prompt_value.strip():
            raise CodexCliRunnerError("codex_cli prompt is invalid")
        if not isinstance(supported, list) or not supported or len(set(supported)) != len(supported):
            raise CodexCliRunnerError("codex_cli supported profiles are invalid")
        if any(profile not in EXECUTION_PROFILES for profile in supported):
            raise CodexCliRunnerError("codex_cli contains an unknown execution profile")
        if not isinstance(profile_values, dict) or set(profile_values) != set(supported):
            raise CodexCliRunnerError("codex_cli profile mapping is incomplete")
        profiles: dict[str, ProfileSettings] = {}
        for profile in supported:
            value = profile_values.get(profile)
            if not isinstance(value, dict):
                raise CodexCliRunnerError("codex_cli profile mapping is invalid")
            model = value.get("model")
            effort = value.get("reasoning_effort")
            if not isinstance(model, str) or not model.strip() or effort not in REASONING_EFFORTS:
                raise CodexCliRunnerError("codex_cli model mapping is invalid")
            profiles[profile] = ProfileSettings(model.strip(), effort)
        prompt_path = (base_dir / prompt_value).resolve()
        if not prompt_path.is_relative_to(base_dir.resolve()) or not prompt_path.is_file():
            raise CodexCliRunnerError("codex_cli prompt path is unavailable")
        sandbox = raw.get("sandbox")
        use_user_config = raw.get("use_user_config")
        timeout = raw.get("timeout_seconds")
        grace = raw.get("termination_grace_seconds")
        stdout_limit = raw.get("max_stdout_chars")
        stderr_limit = raw.get("max_stderr_chars")
        heartbeat = (config.get("task_execution") or {}).get("heartbeat_interval_seconds")
        if sandbox not in SAFE_SANDBOXES:
            raise CodexCliRunnerError("codex_cli sandbox must be read-only or workspace-write")
        if not isinstance(use_user_config, bool):
            raise CodexCliRunnerError("codex_cli use_user_config must be a boolean")
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise CodexCliRunnerError("codex_cli timeout is invalid")
        if not isinstance(grace, (int, float)) or grace <= 0:
            raise CodexCliRunnerError("codex_cli termination grace is invalid")
        if not isinstance(heartbeat, (int, float)) or heartbeat <= 0:
            raise CodexCliRunnerError("heartbeat interval is invalid")
        if not isinstance(stdout_limit, int) or stdout_limit < 1024:
            raise CodexCliRunnerError("codex_cli stdout limit is invalid")
        if not isinstance(stderr_limit, int) or stderr_limit < 1024:
            raise CodexCliRunnerError("codex_cli stderr limit is invalid")
        prefix = command_prefix or cls._resolve_executable(executable, config)
        return cls(
            command_prefix=prefix,
            prompt_path=prompt_path,
            use_user_config=use_user_config,
            sandbox=sandbox,
            timeout_seconds=float(timeout),
            termination_grace_seconds=float(grace),
            heartbeat_interval_seconds=float(heartbeat),
            max_stdout_chars=stdout_limit,
            max_stderr_chars=stderr_limit,
            profiles=profiles,
        )

    @staticmethod
    def _resolve_executable(executable: str, config: dict[str, Any]) -> tuple[str, ...]:
        resolved = shutil.which(executable)
        if not resolved:
            raise CodexCliRunnerError("configured Codex CLI executable was not found")
        path = Path(resolved).resolve()
        workspace = Path(config["workspace"]["task_root"]).resolve()
        if path.is_relative_to(workspace):
            raise CodexCliRunnerError("Codex CLI executable must be outside the task workspace")
        return (str(path),)


@dataclass(frozen=True)
class ProjectContext:
    root: Path
    relative_path: str
    scopes: list[str]


class BoundedText:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.parts: deque[str] = deque()
        self.length = 0
        self.truncated = False
        self.lock = threading.Lock()

    def append(self, value: str) -> None:
        with self.lock:
            self.parts.append(value)
            self.length += len(value)
            while self.length > self.maximum and self.parts:
                excess = self.length - self.maximum
                first = self.parts[0]
                if len(first) <= excess:
                    self.parts.popleft()
                    self.length -= len(first)
                else:
                    self.parts[0] = first[excess:]
                    self.length -= excess
                self.truncated = True

    def value(self) -> str:
        with self.lock:
            return "".join(self.parts)


def sanitize_public_text(value: str, maximum: int = 1000) -> str:
    text = value.replace("\x00", " ")
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    text = re.sub(r"[\r\n]+", " ", text).strip()
    return text[:maximum]


def final_result_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "enum": ["SUCCEEDED", "FAILED", "WAITING_HUMAN"]},
            "summary": {"type": "string", "minLength": 1},
            "verification": {"type": "array", "items": {"type": "string"}},
            "completed": {"type": "array", "items": {"type": "string"}},
            "error": {"type": ["string", "null"]},
            "question": {"type": ["string", "null"]},
            "options": {"type": "array", "items": {"type": "string"}},
            "next_step": {"type": ["string", "null"]},
            "percent": {"type": ["integer", "null"], "minimum": 0, "maximum": 99},
        },
        "required": [
            "status",
            "summary",
            "verification",
            "completed",
            "error",
            "question",
            "options",
            "next_step",
            "percent",
        ],
    }


class CodexCliRunner:
    def __init__(
        self,
        controller: Any,
        config: dict[str, Any],
        settings: CodexCliSettings,
        *,
        logger: SafeLogger | None = None,
    ) -> None:
        self.controller = controller
        self.config = config
        self.settings = settings
        self.workspace = Path(config["workspace"]["task_root"]).resolve()
        self.logger = logger or SafeLogger()
        self._process: subprocess.Popen[str] | None = None

    def run(self, execution_id: str, profile: str) -> dict[str, Any]:
        if not execution_id or profile not in self.settings.profiles:
            raise CodexCliRunnerError("execution id and supported profile must be explicit")
        claim = self.controller.claim(execution_id, RUNTIME_ENVIRONMENT, profile)
        outcome = claim.get("outcome")
        if outcome != "CLAIMED":
            if outcome not in CLAIM_TERMINAL_OUTCOMES:
                raise CodexCliRunnerError("claim returned an unknown outcome")
            self.logger.event("claim_finished", outcome=outcome)
            return claim
        task = claim.get("task")
        if not isinstance(task, dict):
            raise CodexCliRunnerError("claim omitted task")
        task_id = str(task.get("id") or "")
        if task.get("runtime_environment") != RUNTIME_ENVIRONMENT or task.get("execution_profile") != profile:
            return self._finish(execution_id, task_id, self._failed("claimed task routing does not match Codex CLI"))
        try:
            project = self._project_context(task)
        except Exception as error:
            return self._finish(
                execution_id,
                task_id,
                self._waiting(
                    "任务 scope 无法安全映射到单个登记项目。",
                    f"请修正任务 scope 后重新排队：{sanitize_public_text(str(error), 500)}",
                ),
            )

        def heartbeat() -> Any:
            return self.controller.heartbeat(execution_id, task_id)

        try:
            with HeartbeatGuard(heartbeat, self.settings.heartbeat_interval_seconds, self.logger) as guard:
                result = self._execute(task, project, profile, guard)
                guard.ensure_healthy()
                guard.beat()
        except KeyboardInterrupt:
            self._terminate_active_process()
            result = self._failed("Codex CLI execution was interrupted")
        except Exception as error:
            self._terminate_active_process()
            self.logger.event("codex_cli_failed", error=type(error).__name__)
            result = self._failed(self._public_error(error))
        return self._finish(execution_id, task_id, result)

    def _project_context(self, task: dict[str, Any]) -> ProjectContext:
        scopes = task.get("scope")
        if not isinstance(scopes, list) or not scopes or not all(isinstance(item, str) and item.strip() for item in scopes):
            raise CodexCliRunnerError("claimed task has invalid scope")
        ScopePolicy(self.workspace, scopes)
        projects = configured_projects(self.config)
        project_paths = [item["path"] for item in projects]
        keys = {resolve_scope_key(scope, project_paths) for scope in scopes}
        if len(keys) != 1:
            raise CodexCliRunnerError("multiple projects are not supported")
        key = next(iter(keys))
        if not key.startswith("project:"):
            raise CodexCliRunnerError("external scope is not supported")
        relative = key.removeprefix("project:")
        record = next((item for item in projects if item["path"] == relative), None)
        if record is None or not record["exists_on_disk"]:
            raise CodexCliRunnerError("registered project directory is unavailable")
        root = (self.workspace / Path(relative)).resolve()
        if not root.is_dir() or not root.is_relative_to(self.workspace):
            raise CodexCliRunnerError("project working directory is unsafe")
        for scope in scopes:
            target = (self.workspace / Path(scope)).resolve()
            if not target.is_relative_to(root):
                raise CodexCliRunnerError("scope escapes the resolved project")
        return ProjectContext(root=root, relative_path=relative, scopes=list(scopes))

    def _execute(
        self,
        task: dict[str, Any],
        project: ProjectContext,
        profile: str,
        guard: HeartbeatGuard,
    ) -> dict[str, Any]:
        profile_settings = self.settings.profiles[profile]
        prompt = self._build_prompt(task, project)
        stdout = BoundedText(self.settings.max_stdout_chars)
        stderr = BoundedText(self.settings.max_stderr_chars)
        with tempfile.TemporaryDirectory(prefix="local-agent-loop-codex-") as temporary:
            schema_path = Path(temporary) / "final-result.schema.json"
            schema_path.write_text(
                json.dumps(final_result_schema(), ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
            )
            command = [
                *self.settings.command_prefix,
                "exec",
                "--json",
                "--ephemeral",
                *([] if self.settings.use_user_config else ["--ignore-user-config"]),
                "--color",
                "never",
                "--model",
                profile_settings.model,
                "--sandbox",
                self.settings.sandbox,
                "--cd",
                str(project.root),
                "--output-schema",
                str(schema_path),
                "-c",
                f'model_reasoning_effort="{profile_settings.reasoning_effort}"',
                "-",
            ]
            self.logger.event("codex_cli_started", profile=profile, project=project.relative_path)
            process = self._start_process(command)
            self._process = process
            readers = [
                self._reader(process.stdout, stdout, "stdout"),
                self._reader(process.stderr, stderr, "stderr"),
            ]
            assert process.stdin is not None
            try:
                process.stdin.write(prompt)
                process.stdin.close()
                deadline = time.monotonic() + self.settings.timeout_seconds
                while process.poll() is None:
                    guard.ensure_healthy()
                    if time.monotonic() >= deadline:
                        raise CodexCliTimeout("Codex CLI execution timed out")
                    time.sleep(0.05)
                return_code = process.returncode
            finally:
                if process.poll() is None:
                    self._terminate_process_tree(process)
                for reader in readers:
                    reader.join(timeout=self.settings.termination_grace_seconds)
                self._process = None
        self.logger.event(
            "codex_cli_finished",
            return_code=return_code,
            stdout_truncated=stdout.truncated,
            stderr_truncated=stderr.truncated,
        )
        if return_code != 0:
            public_error = sanitize_public_text(stderr.value()) or f"Codex CLI exited with code {return_code}"
            if AUTH_ERROR.search(public_error):
                return self._waiting(
                    "Codex CLI 账户、登录状态或模型权限不可用。",
                    "请在 Runner 外部恢复 Codex CLI 登录或模型权限后重新排队。",
                )
            return self._failed(f"Codex CLI exited with code {return_code}: {public_error}")
        return self._parse_final_result(stdout.value())

    def _start_process(self, command: list[str]) -> subprocess.Popen[str]:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "shell": False,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            return subprocess.Popen(command, **kwargs)
        except OSError as error:
            raise CodexCliRunnerError("Codex CLI process could not be started") from error

    def _reader(self, stream: TextIO | None, capture: BoundedText, name: str) -> threading.Thread:
        if stream is None:
            raise CodexCliRunnerError(f"Codex CLI {name} pipe is unavailable")

        def read() -> None:
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    capture.append(chunk)
            finally:
                stream.close()

        thread = threading.Thread(target=read, name=f"codex-cli-{name}", daemon=True)
        thread.start()
        return thread

    def _terminate_active_process(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            self._terminate_process_tree(process)
        self._process = None

    def _terminate_process_tree(self, process: subprocess.Popen[str]) -> None:
        self.logger.event("codex_cli_terminating", pid=process.pid)
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=self.settings.termination_grace_seconds,
                    check=False,
                )
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=self.settings.termination_grace_seconds)
        except (OSError, subprocess.SubprocessError):
            try:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait(timeout=self.settings.termination_grace_seconds)
            except (OSError, subprocess.SubprocessError):
                self.logger.event("codex_cli_termination_failed", pid=process.pid)

    def _parse_final_result(self, output: str) -> dict[str, Any]:
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
                if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                    candidates.append(item["text"])
            elif event.get("type") == "agent_message" and isinstance(event.get("text"), str):
                candidates.append(event["text"])
        for candidate in reversed(candidates):
            try:
                value = json.loads(candidate)
                return validate_final_result(value)
            except (json.JSONDecodeError, AgentRuntimeError):
                continue
        raise CodexCliRunnerError("Codex CLI produced no valid final result")

    def _build_prompt(self, task: dict[str, Any], project: ProjectContext) -> str:
        authority = self.settings.prompt_path.read_text(encoding="utf-8")
        payload = {
            "id": task.get("id"),
            "description": task.get("description") or "",
            "scope": project.scopes,
            "acceptance": list(task.get("acceptance") or []),
            "dependencies": [
                {"id": dependency, "state": "satisfied_before_claim"}
                for dependency in task.get("depends_on") or []
            ],
            "project": project.relative_path,
        }
        return f"{authority.rstrip()}\n\n# 当前任务\n\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n"

    def _finish(self, execution_id: str, task_id: str, result: dict[str, Any]) -> dict[str, Any]:
        validated = validate_final_result(result)
        finish = self.controller.finish(execution_id, task_id, validated)
        if finish.get("outcome") != "FINISHED":
            raise CodexCliRunnerError("finish did not confirm task update")
        return {"outcome": "FINISHED", "task_id": task_id, "result": validated, "finish": finish}

    @staticmethod
    def _failed(error: str) -> dict[str, Any]:
        return {
            "status": "FAILED",
            "summary": "Codex CLI Runner 本轮执行失败。",
            "error": sanitize_public_text(error, 4000),
        }

    @staticmethod
    def _waiting(summary: str, question: str) -> dict[str, Any]:
        return {
            "status": "WAITING_HUMAN",
            "summary": summary[:4000],
            "question": question[:4000],
            "options": ["修正外部条件后重新排队", "保持等待"],
            "next_step": "等待人工处理后重新排队。",
            "percent": 0,
        }

    @staticmethod
    def _public_error(error: Exception) -> str:
        if isinstance(error, (CodexCliRunnerError, AgentRuntimeError)):
            return sanitize_public_text(str(error))
        return f"runner error: {type(error).__name__}"


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Single-task Local Agent Loop Codex CLI Runner")
    root.add_argument("--profile", required=True, choices=EXECUTION_PROFILES)
    root.add_argument("--config", default=str(CONFIG_PATH))
    root.add_argument("--db")
    return root


def main() -> None:
    args = parser().parse_args()
    config = load_initialization_config(Path(args.config))
    settings = CodexCliSettings.from_config(config)
    if args.profile not in settings.profiles:
        raise CodexCliRunnerError("execution profile is not enabled for Codex CLI")
    execution_id = f"codex-cli-{args.profile}-{uuid.uuid4()}"
    runner = CodexCliRunner(
        SubprocessLoopController(Path(args.db) if args.db else None),
        config,
        settings,
    )
    result = runner.run(execution_id, args.profile)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            json.dumps(
                {"outcome": "RUNNER_ERROR", "error": CodexCliRunner._public_error(error)},
                ensure_ascii=False,
            )
        )
        raise SystemExit(1)
