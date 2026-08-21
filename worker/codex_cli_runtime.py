"""Runner 启动的 Codex CLI 单任务 Worker。

Runner 根据能力等级解析固定模型与推理配置，通过 loopctl 原子领取一个任务，把任务
限制到单个已登记项目和 Planner 声明的 scope 内，再启动隔离的 ``codex exec`` 子进程。
执行期间后台心跳维持 execution 与锁租约；标准输出按 JSONL 解析，最终结果还要经过
统一协议校验后才能 finish。

重试只发生在没有观察到命令执行、文件修改或 MCP 调用等副作用时。一旦可能已经写入，
Runner 会抑制自动重试，避免同一操作重复执行。超时或异常清理仅针对当前精确进程树，
不按进程名进行全局终止。
"""

from __future__ import annotations

# 中文排查：Runner 负责单次 claim、Codex 子进程、heartbeat、超时、结果解析与 finish。
# 故障按“配置 -> claim -> 项目/scope -> 子进程 JSONL -> 终态回写”顺序定位。
# 进程清理必须基于当前 execution 的精确 PID/进程树，不能按进程名或端口模糊终止。

import argparse
import json
import re
import shutil
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

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = REPOSITORY_ROOT / "control"
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.append(str(REPOSITORY_ROOT))

from loop_agent.runtime.controller import HeartbeatGuard, SubprocessLoopController
from loop_agent.runtime.core import ExecutionProfile, SafeLogger
from loop_agent.runtime.diagnostics import AgentRuntimeError
from loop_agent.runtime.protocol import validate_final_result
from loop_agent.runtime.sandbox import ScopePolicy
from loopdb import (
    BASE_DIR,
    CAPABILITY_LEVELS,
    CONFIG_PATH,
    configured_projects,
    load_initialization_config,
    resolve_scope_key,
)


RUNTIME_ENVIRONMENT = "codex_cli"
CLAIM_TERMINAL_OUTCOMES = {"NO_TASK", "SLOT_FULL", "CONFLICT"}
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
    """Runner 配置、边界、协议或进程生命周期违反约束时的基础异常。"""

    pass


class CodexCliAttemptError(CodexCliRunnerError):
    """单次 Codex CLI 尝试失败，并显式携带是否允许安全重试。"""

    def __init__(self, message: str, *, retryable: bool) -> None:
        """保存可公开错误信息与重试判定，供外层尝试循环决策。"""
        self.retryable = retryable
        super().__init__(message)


@dataclass(frozen=True)
class CodexCliSettings:
    """Codex CLI Runner 的不可变配置快照。

    包含可信可执行文件、权威提示词、sandbox、终止宽限、心跳/停滞阈值及输出上限。
    构造后本轮 execution 不再读取这些值，防止运行中配置变化导致行为漂移。
    """
    command_prefix: tuple[str, ...]
    prompt_path: Path
    use_user_config: bool
    sandbox: str
    termination_grace_seconds: float
    heartbeat_interval_seconds: float
    stalled_after_seconds: float
    max_stdout_chars: int
    max_stderr_chars: int
    process_poll_interval_seconds: float

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        *,
        base_dir: Path = BASE_DIR,
        command_prefix: tuple[str, ...] | None = None,
    ) -> "CodexCliSettings":
        """校验配置、全部能力档位和时间关系，并解析安全的绝对路径。"""
        raw = config.get("codex_cli")
        if not isinstance(raw, dict):
            raise CodexCliRunnerError("codex_cli configuration is missing")
        executable = raw.get("executable")
        prompt_value = raw.get("prompt")
        if not isinstance(executable, str) or not executable.strip():
            raise CodexCliRunnerError("codex_cli executable is invalid")
        if not isinstance(prompt_value, str) or not prompt_value.strip():
            raise CodexCliRunnerError("codex_cli prompt is invalid")
        profiles = {
            level: ExecutionProfile.resolve(config, RUNTIME_ENVIRONMENT, None, level)
            for level in CAPABILITY_LEVELS
        }
        prompt_path = (base_dir / prompt_value).resolve()
        if not prompt_path.is_relative_to(base_dir.resolve()) or not prompt_path.is_file():
            raise CodexCliRunnerError("codex_cli prompt path is unavailable")
        sandbox = raw.get("sandbox")
        use_user_config = raw.get("use_user_config")
        grace = raw.get("termination_grace_seconds")
        stdout_limit = raw.get("max_stdout_chars")
        stderr_limit = raw.get("max_stderr_chars")
        poll_interval = raw.get("process_poll_interval_seconds")
        heartbeat = (config.get("task_execution") or {}).get("heartbeat_interval_seconds")
        stalled = (config.get("task_execution") or {}).get("stalled_after_seconds")
        if sandbox not in SAFE_SANDBOXES:
            raise CodexCliRunnerError("codex_cli sandbox must be read-only or workspace-write")
        if not isinstance(use_user_config, bool):
            raise CodexCliRunnerError("codex_cli use_user_config must be a boolean")
        if not isinstance(grace, (int, float)) or grace <= 0:
            raise CodexCliRunnerError("codex_cli termination grace is invalid")
        if not isinstance(heartbeat, (int, float)) or heartbeat <= 0:
            raise CodexCliRunnerError("heartbeat interval is invalid")
        if not isinstance(stalled, (int, float)) or not heartbeat < stalled:
            raise CodexCliRunnerError("heartbeat interval must be below stalled detection")
        if any(profile.attempt_timeout_seconds <= float(stalled) for profile in profiles.values()):
            raise CodexCliRunnerError("stalled detection must be below every Codex CLI attempt timeout")
        if not isinstance(stdout_limit, int) or stdout_limit < 1024:
            raise CodexCliRunnerError("codex_cli stdout limit is invalid")
        if not isinstance(stderr_limit, int) or stderr_limit < 1024:
            raise CodexCliRunnerError("codex_cli stderr limit is invalid")
        if not isinstance(poll_interval, (int, float)) or not 0 < poll_interval <= 5:
            raise CodexCliRunnerError("codex_cli process poll interval is invalid")
        prefix = command_prefix or cls._resolve_executable(executable, config)
        return cls(
            command_prefix=prefix,
            prompt_path=prompt_path,
            use_user_config=use_user_config,
            sandbox=sandbox,
            termination_grace_seconds=float(grace),
            heartbeat_interval_seconds=float(heartbeat),
            stalled_after_seconds=float(stalled),
            max_stdout_chars=stdout_limit,
            max_stderr_chars=stderr_limit,
            process_poll_interval_seconds=float(poll_interval),
        )

    @staticmethod
    def _resolve_executable(executable: str, config: dict[str, Any]) -> tuple[str, ...]:
        """从 PATH 定位 Codex CLI，并拒绝位于任务工作区内的可执行文件。"""
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
    """一次任务解析出的唯一项目根目录、登记路径和原始 scope 列表。"""
    root: Path
    relative_path: str
    scopes: list[str]


def _codex_command(
    settings: CodexCliSettings,
    execution_profile: ExecutionProfile,
    project_root: Path,
    schema_path: Path,
) -> list[str]:
    """构建正式 Worker 的 Codex CLI 命令，供执行器和边界测试共用。"""
    return [
        *settings.command_prefix,
        "exec",
        "--json",
        "--ephemeral",
        *([] if settings.use_user_config else ["--ignore-user-config"]),
        "--color",
        "never",
        "--model",
        execution_profile.model,
        "--sandbox",
        settings.sandbox,
        "--cd",
        str(project_root),
        "--output-schema",
        str(schema_path),
        "-c",
        f'model_reasoning_effort="{execution_profile.reasoning}"',
        "-",
    ]


class BoundedText:
    """线程安全的尾部文本缓冲区，超过上限时丢弃最旧内容。

    子进程可能持续输出，限制缓冲可避免长期任务耗尽内存；``truncated`` 用于日志标记
    内容曾被裁剪，但不会把被裁剪的敏感文本写入其他位置。
    """

    def __init__(self, maximum: int) -> None:
        """创建最多保留 ``maximum`` 个字符的空缓冲区。"""
        self.maximum = maximum
        self.parts: deque[str] = deque()
        self.length = 0
        self.truncated = False
        self.lock = threading.Lock()

    def append(self, value: str) -> None:
        """追加文本并从头部精确裁剪超出上限的字符。"""
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
        """在锁内合并并返回当前保留的文本快照。"""
        with self.lock:
            return "".join(self.parts)


def sanitize_public_text(value: str, maximum: int = 1000) -> str:
    """清除密钥、认证头、Codex 私有路径、换行和空字符后截断公开文本。"""
    text = value.replace("\x00", " ")
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    text = re.sub(r"[\r\n]+", " ", text).strip()
    return text[:maximum]


def final_result_schema() -> dict[str, Any]:
    """返回传给 Codex CLI 的严格最终结果 JSON Schema。

    Schema 禁止额外字段，并限制三种终态及等待人工时的进度范围；Runtime 仍会用
    ``validate_final_result`` 做第二次语义校验。
    """
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
    """编排单个 Codex CLI execution 的完整生命周期。"""

    def __init__(
        self,
        controller: Any,
        config: dict[str, Any],
        settings: CodexCliSettings,
        *,
        logger: SafeLogger | None = None,
    ) -> None:
        """注入 loopctl 控制器、配置快照、Runner 设置和安全事件日志器。"""
        self.controller = controller
        self.config = config
        self.settings = settings
        self.workspace = Path(config["workspace"]["task_root"]).resolve()
        self.logger = logger or SafeLogger()
        self._process: subprocess.Popen[str] | None = None

    def run(self, execution_id: str, capability_level: str) -> dict[str, Any]:
        """领取并执行一个匹配能力等级的任务，然后保证写入一个最终结果。

        claim 的 NO_TASK/SLOT_FULL/CONFLICT 直接作为正常结果返回。领取成功后严格复核
        环境、Provider 和等级，解析唯一项目；项目边界错误转为 WAITING_HUMAN。执行期
        心跳失败、进程异常或中断统一清理当前进程树，最后通过 finish 关闭 execution。
        """
        if not execution_id or capability_level not in CAPABILITY_LEVELS:
            raise CodexCliRunnerError("execution id and capability level must be explicit")
        configured_profile = ExecutionProfile.resolve(
            self.config, RUNTIME_ENVIRONMENT, None, capability_level
        )
        claim = self.controller.claim(
            execution_id, RUNTIME_ENVIRONMENT, capability_level, None
        )
        outcome = claim.get("outcome")
        if outcome != "CLAIMED":
            if outcome not in CLAIM_TERMINAL_OUTCOMES:
                raise CodexCliRunnerError("claim returned an unknown outcome")
            self.logger.event("claim_finished", outcome=outcome)
            return claim
        execution_profile = (
            ExecutionProfile.from_snapshot(claim["execution_profile"])
            if "execution_profile" in claim
            else configured_profile
        )
        task = claim.get("task")
        if not isinstance(task, dict):
            raise CodexCliRunnerError("claim omitted task")
        task_id = str(task.get("id") or "")
        if (
            task.get("runtime_environment") != RUNTIME_ENVIRONMENT
            or task.get("provider_id") is not None
            or task.get("capability_level") != capability_level
        ):
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
            """把 HeartbeatGuard 的周期回调转发给当前 execution/task。"""
            return self.controller.heartbeat(execution_id, task_id)

        try:
            with HeartbeatGuard(heartbeat, self.settings.heartbeat_interval_seconds, self.logger) as guard:
                result = self._run_attempts(task, project, execution_profile, guard)
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

    def _run_attempts(
        self,
        task: dict[str, Any],
        project: ProjectContext,
        execution_profile: ExecutionProfile,
        guard: HeartbeatGuard,
    ) -> dict[str, Any]:
        """按档位重试预算执行 Codex CLI，并在可能有副作用后禁止重试。

        每次尝试前后都确认心跳健康。只有 FAILED 且输出没有副作用证据时才进入下一次；
        SUCCEEDED 和 WAITING_HUMAN 立即结束。最后一次失败始终原样返回。
        """
        maximum_attempts = execution_profile.max_retries + 1
        last_result = self._failed("Codex CLI attempt did not start")
        for attempt in range(1, maximum_attempts + 1):
            guard.ensure_healthy()
            guard.beat()
            self.logger.event(
                "codex_cli_attempt_started", attempt=attempt,
                maximum_attempts=maximum_attempts,
                capability_level=execution_profile.capability_level,
            )
            try:
                last_result, side_effects = self._execute(
                    task, project, execution_profile, guard, attempt
                )
            except CodexCliAttemptError as error:
                guard.ensure_healthy()
                last_result = self._failed(str(error))
                side_effects = not error.retryable
            if last_result.get("status") != "FAILED":
                return last_result
            retryable = not side_effects
            if not retryable or attempt >= maximum_attempts:
                if not retryable and attempt < maximum_attempts:
                    last_result = self._failed(
                        str(last_result.get("error") or "Codex CLI attempt failed")
                        + "; execution retry suppressed after possible side effects"
                    )
                return last_result
            guard.ensure_healthy()
            guard.beat()
            self.logger.event("codex_cli_attempt_retry", attempt=attempt, next_attempt=attempt + 1)
        return last_result

    def _project_context(self, task: dict[str, Any]) -> ProjectContext:
        """把任务 scope 解析为单个已登记且存在的项目工作目录。

        先由 ScopePolicy 校验工作区边界，再要求所有 scope_key 指向同一 project，拒绝
        external scope、多项目任务、磁盘缺失项目及任何逃逸项目根目录的路径。
        """
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
        execution_profile: ExecutionProfile,
        guard: HeartbeatGuard,
        attempt: int,
    ) -> tuple[dict[str, Any], bool]:
        """启动一次 ``codex exec``，监控超时并返回最终报告与副作用标记。

        临时目录只存最终结果 Schema。提示词经 stdin 传入，stdout/stderr 由独立线程
        有界采集。退出后先根据 JSONL 事件识别副作用，再区分认证问题、普通非零退出、
        超时和最终结果解析失败，以决定等待人工或是否允许重试。
        """
        prompt = self._build_prompt(task, project, attempt)
        stdout = BoundedText(self.settings.max_stdout_chars)
        stderr = BoundedText(self.settings.max_stderr_chars)
        timed_out = False
        return_code: int | None = None
        with tempfile.TemporaryDirectory(prefix="local-agent-loop-codex-") as temporary:
            schema_path = Path(temporary) / "final-result.schema.json"
            schema_path.write_text(
                json.dumps(final_result_schema(), ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
            )
            command = _codex_command(
                self.settings, execution_profile, project.root, schema_path
            )
            self.logger.event(
                "codex_cli_started", capability_level=execution_profile.capability_level,
                attempt=attempt, project=project.relative_path,
            )
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
                deadline = time.monotonic() + execution_profile.attempt_timeout_seconds
                while process.poll() is None:
                    guard.ensure_healthy()
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break
                    time.sleep(self.settings.process_poll_interval_seconds)
                return_code = process.poll()
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
        side_effects = self._output_may_have_side_effects(stdout.value())
        if timed_out:
            raise CodexCliAttemptError(
                "Codex CLI attempt timed out",
                retryable=not side_effects,
            )
        if return_code != 0:
            public_error = sanitize_public_text(stderr.value()) or f"Codex CLI exited with code {return_code}"
            if AUTH_ERROR.search(public_error):
                return self._waiting(
                    "Codex CLI 账户、登录状态或模型权限不可用。",
                    "请在 Runner 外部恢复 Codex CLI 登录或模型权限后重新排队。",
                ), side_effects
            raise CodexCliAttemptError(
                f"Codex CLI exited with code {return_code}: {public_error}",
                retryable=not side_effects,
            )
        try:
            return self._parse_final_result(stdout.value()), side_effects
        except CodexCliRunnerError as error:
            raise CodexCliAttemptError(str(error), retryable=not side_effects) from error

    def _start_process(self, command: list[str]) -> subprocess.Popen[str]:
        """以 UTF-8 管道和 Windows 独立进程组启动 Codex CLI，始终禁用 shell 解析。"""
        kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "shell": False,
            "env": safe_subprocess_environment(),
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
        }
        try:
            return subprocess.Popen(command, **kwargs)
        except OSError as error:
            raise CodexCliRunnerError("Codex CLI process could not be started") from error

    def _reader(self, stream: TextIO | None, capture: BoundedText, name: str) -> threading.Thread:
        """启动守护线程分块读取一个子进程管道，并在结束时可靠关闭流。"""
        if stream is None:
            raise CodexCliRunnerError(f"Codex CLI {name} pipe is unavailable")

        def read() -> None:
            """持续读取 4096 字符块到有界缓冲，直到 EOF。"""
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
        """若当前精确子进程仍活动，则终止其进程树并清空内部引用。"""
        process = self._process
        if process is not None and process.poll() is None:
            self._terminate_process_tree(process)
        self._process = None

    def _terminate_process_tree(self, process: subprocess.Popen[str]) -> None:
        """终止给定 PID 的进程树，宽限期后升级为强制终止。

        使用 ``taskkill /PID /T /F`` 终止 Windows 进程树；命令失败时再终止精确子进程。
        两轮终止后仍存活会抛错，禁止遗留失去心跳和锁续期的写进程。
        """
        self.logger.event("codex_cli_terminating", pid=process.pid)
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self.settings.termination_grace_seconds,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            process.wait(timeout=self.settings.termination_grace_seconds)
        except (OSError, subprocess.SubprocessError):
            try:
                process.kill()
                process.wait(timeout=self.settings.termination_grace_seconds)
            except (OSError, subprocess.SubprocessError):
                self.logger.event("codex_cli_termination_failed", pid=process.pid)
        if process.poll() is None:
            raise CodexCliRunnerError("Codex CLI process tree could not be terminated")

    @staticmethod
    def _output_may_have_side_effects(output: str) -> bool:
        """从 Codex JSONL 事件保守判断本次尝试是否可能执行过写操作。"""
        side_effect_types = {"command_execution", "file_change", "mcp_tool_call"}
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") in side_effect_types:
                return True
            if event.get("type") in {"file_change", "command_execution"}:
                return True
        return False

    def _parse_final_result(self, output: str) -> dict[str, Any]:
        """从最新 Agent 消息向前查找首个合法 JSON 最终结果并做协议校验。"""
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

    def _build_prompt(self, task: dict[str, Any], project: ProjectContext, attempt: int) -> str:
        """拼接权威 Runner 提示词与当前任务最小 JSON 载荷。

        依赖只暴露“领取前已满足”，不把数据库内部状态传给模型；项目使用登记相对路径，
        scope 和验收项保持 Planner 发布的顺序。
        """
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
            "execution_attempt": attempt,
        }
        return f"{authority.rstrip()}\n\n# 当前任务\n\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n"

    def _finish(self, execution_id: str, task_id: str, result: dict[str, Any]) -> dict[str, Any]:
        """再次校验最终结果并要求 loopctl 明确确认 FINISHED 后才返回成功。"""
        validated = validate_final_result(result)
        finish = self.controller.finish(execution_id, task_id, validated)
        if finish.get("outcome") != "FINISHED":
            raise CodexCliRunnerError("finish did not confirm task update")
        return {"outcome": "FINISHED", "task_id": task_id, "result": validated, "finish": finish}

    @staticmethod
    def _failed(error: str) -> dict[str, Any]:
        """把异常转为经过脱敏和长度限制的标准 FAILED 报告。"""
        return {
            "status": "FAILED",
            "summary": "Codex CLI Runner 本轮执行失败。",
            "error": sanitize_public_text(error, 4000),
        }

    @staticmethod
    def _waiting(summary: str, question: str) -> dict[str, Any]:
        """构造因外部条件无法自动修复而等待人工处理的标准报告。"""
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
        """仅公开白名单异常正文；未知异常只暴露类型，不泄露堆栈或本机路径。"""
        if isinstance(error, (CodexCliRunnerError, AgentRuntimeError)):
            return sanitize_public_text(str(error))
        return f"runner error: {type(error).__name__}"


def parser() -> argparse.ArgumentParser:
    """构建单任务 Codex CLI Runner 命令行。"""
    root = argparse.ArgumentParser(description="Single-task Local Agent Loop Codex CLI Runner")
    root.add_argument("--execution-id", required=True)
    root.add_argument("--capability-level", required=True, choices=CAPABILITY_LEVELS)
    root.add_argument("--config", default=str(CONFIG_PATH))
    root.add_argument("--db")
    return root


def main() -> None:
    """加载配置并执行 Runner 已经排入队列的唯一 execution。"""
    args = parser().parse_args()
    config = load_initialization_config(Path(args.config))
    settings = CodexCliSettings.from_config(config)
    runner = CodexCliRunner(
        SubprocessLoopController(
            Path(args.db) if args.db else None,
            timeout_seconds=float(config["task_execution"]["controller_timeout_seconds"]),
        ),
        config,
        settings,
    )
    result = runner.run(args.execution_id, args.capability_level)
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
