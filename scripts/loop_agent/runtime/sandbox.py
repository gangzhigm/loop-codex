"""Filesystem scope enforcement and the self-hosted Agent tool sandbox.

This is the security-sensitive local I/O boundary. All model-supplied paths are
resolved relative to the configured workspace, checked against the exact scope
claimed from SQLite, and rejected when they address credentials, VCS internals,
or other sensitive locations. Text reads and writes are strict UTF-8.

Only read-only ``git``/``rg`` commands are allowed. They execute without a
shell, with restricted arguments, a sanitized environment, and binaries that
must resolve outside the task workspace.
"""

from __future__ import annotations

# 中文排查：ScopePolicy 解析允许路径，ToolSandbox 执行受限读取、写入、搜索和命令。
# 路径拒绝先检查项目根、标准化 scope 和重解析点；命令拒绝再检查允许动作和参数。
# 任何新增工具都必须先设计路径、凭据、删除和进程归属边界，不能只增加功能实现。

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from loop_agent.runtime.contracts import (
    HIGH_RISK_ACTIONS,
    SENSITIVE_COMPONENT,
    SHELL_META,
)
from loop_agent.runtime.core import (
    AgentRuntimeError,
    ApprovalRequired,
    RuntimeSettings,
    ToolRejected,
)


class ScopePolicy:
    """Resolve model paths against the immutable scopes of a claimed task."""

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
        """Detect control data, VCS internals, credential stores, and key files."""
        for part in parts:
            lowered = part.lower()
            if (
                lowered
                in {
                    ".reasonix",
                    "$codex_home",
                    ".git",
                    ".hg",
                    ".svn",
                    ".ssh",
                    ".aws",
                    ".azure",
                    ".kube",
                    ".npmrc",
                    ".pypirc",
                    ".netrc",
                    "id_rsa",
                    "id_ed25519",
                }
                or lowered == ".env"
                or lowered.startswith(".env.")
            ):
                return True
            if lowered.endswith((".pem", ".p12", ".pfx", ".key")) or SENSITIVE_COMPONENT.search(part):
                return True
        return False

    def resolve(
        self,
        value: str,
        *,
        must_exist: bool = False,
        directory: bool | None = None,
    ) -> Path:
        """Return one safe absolute path or reject before any I/O occurs."""
        if not isinstance(value, str) or not value.strip():
            raise ToolRejected("path must be a non-empty relative path")
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ToolRejected("absolute and parent-relative paths are forbidden")
        if self._is_sensitive(candidate.parts):
            raise ToolRejected("sensitive paths are forbidden")
        resolved = (self.workspace / candidate).resolve()
        allowed = any(
            (
                directory_scope
                and (resolved == root or resolved.is_relative_to(root))
            )
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
        """Find applicable AGENTS.md files from workspace root to scope roots."""
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
        return sorted(
            candidates, key=lambda item: (len(item.parts), str(item).lower())
        )


class ToolSandbox:
    """Execute the small, explicit tool protocol exposed to a model provider."""

    TOOL_SCHEMAS = [
        {"name": "read_file", "arguments": {"path": "relative UTF-8 file path"}},
        {
            "name": "search",
            "arguments": {"path": "relative directory", "pattern": "regular expression"},
        },
        {
            "name": "apply_patch",
            "arguments": {
                "path": "relative file",
                "old": "exact text",
                "new": "replacement",
            },
        },
        {
            "name": "run_command",
            "arguments": {"argv": "safe argv array", "cwd": "relative directory"},
        },
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
        # Retry logic uses this counter to avoid replaying an attempt after any
        # local write may already have succeeded.
        self.side_effect_count = 0

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
        return text[:maximum], len(text) > maximum

    def _read_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self.policy.resolve(
            str(arguments.get("path", "")), must_exist=True, directory=False
        )
        if path.stat().st_size > self.settings.max_file_bytes:
            raise ToolRejected("file exceeds configured read limit")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise ToolRejected("file is not valid UTF-8 text") from error
        content, truncated = self._bounded(text)
        return {
            "path": path.relative_to(self.policy.workspace).as_posix(),
            "content": content,
            "truncated": truncated,
        }

    def _search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        directory = self.policy.resolve(
            str(arguments.get("path", "")), must_exist=True, directory=True
        )
        pattern_text = arguments.get("pattern")
        if (
            not isinstance(pattern_text, str)
            or not pattern_text
            or len(pattern_text) > 500
        ):
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
                checked = self.policy.resolve(
                    relative, must_exist=True, directory=False
                )
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
                    matches.append(
                        {"path": relative, "line": number, "text": excerpt}
                    )
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
            self.side_effect_count += 1
            return {
                "path": path.relative_to(self.policy.workspace).as_posix(),
                "changed": True,
                "created": True,
            }
        if not old:
            raise ToolRejected(
                "empty old text is only valid when creating a missing file"
            )
        if path.exists() and path.stat().st_size > self.settings.max_file_bytes:
            raise ToolRejected("file exceeds configured edit limit")
        try:
            text = path.read_text(encoding="utf-8") if path.exists() else ""
        except UnicodeDecodeError as error:
            raise ToolRejected("file is not valid UTF-8 text") from error
        occurrences = text.count(old)
        if occurrences != 1:
            raise ToolRejected(
                f"patch old text must match exactly once; matched {occurrences}"
            )
        updated = text.replace(old, new, 1)
        if len(updated.encode("utf-8")) > self.settings.max_file_bytes:
            raise ToolRejected("patched file exceeds configured edit limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated, encoding="utf-8", newline="")
        self.side_effect_count += 1
        return {
            "path": path.relative_to(self.policy.workspace).as_posix(),
            "changed": True,
        }

    def _run_command(self, arguments: dict[str, Any]) -> dict[str, Any]:
        argv = arguments.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) and item for item in argv)
        ):
            raise ToolRejected("argv must be a non-empty string array")
        if len(argv) > 32 or any(
            SHELL_META.search(item) or "\x00" in item for item in argv
        ):
            raise ToolRejected(
                "shell metacharacters and oversized commands are forbidden"
            )
        cwd_value = arguments.get("cwd")
        cwd = self.policy.resolve(
            str(cwd_value or ""), must_exist=True, directory=True
        )
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
        """Translate model git requests into fixed read-only invocations."""
        if len(argv) < 2 or argv[1] not in {"status", "diff"}:
            raise ToolRejected("only git status and git diff are allowed")
        if argv[1] == "status":
            if argv[2:] not in ([], ["--short"]):
                raise ToolRejected("git status only accepts --short")
            return [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "--no-pager",
                "status",
                "--short",
                "--ignore-submodules=all",
            ]
        allowed = {"--check", "--stat", "--name-only", "--name-status"}
        if any(item not in allowed for item in argv[2:]):
            raise ToolRejected("git diff arguments are restricted")
        return [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "diff.external=",
            "--no-pager",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=all",
            *argv[2:],
        ]

    def _safe_rg(self, argv: list[str], cwd: Path) -> list[str]:
        """Reject rg features that execute preprocessors or leave scope."""
        if len(argv) < 2:
            raise ToolRejected("rg requires a pattern")
        forbidden = {
            "--pre",
            "--pre-glob",
            "--search-zip",
            "--follow",
            "-L",
            "--hostname-bin",
            "--hidden",
            "--no-ignore",
            "--no-ignore-vcs",
            "--unrestricted",
            "-u",
            "-uu",
            "-uuu",
        }
        if any(
            item in forbidden or item.startswith("--pre=") for item in argv[1:]
        ):
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
            "rg",
            "--no-config",
            "--color",
            "never",
            *argv[1:],
            "-g",
            "!.env*",
            "-g",
            "!**/.env*",
            "-g",
            "!**/.git/**",
            "-g",
            "!**/.reasonix/**",
            "-g",
            "!**/*secret*",
            "-g",
            "!**/*credential*",
            "-g",
            "!**/*private-key*",
        ]

    def _trusted_executable(self, name: str) -> Path:
        """Resolve an allowlisted binary and reject workspace shadowing."""
        resolved_name = shutil.which(name, path=os.environ.get("PATH", ""))
        if not resolved_name:
            raise ToolRejected("allowed command is not installed")
        executable = Path(resolved_name).resolve()
        if executable.is_relative_to(self.policy.workspace):
            raise ToolRejected("command executable cannot come from the workspace")
        return executable
