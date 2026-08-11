"""Host control-plane adapter and heartbeat lifecycle for one Agent run."""

from __future__ import annotations

# 中文排查：本模块封装 loopctl 子进程调用，并用 HeartbeatGuard 维护后台心跳。
# 控制面调用失败先检查命令、UTF-8 JSON 和返回码；心跳失败再检查线程保存的首个异常。
# guard 退出前必须停止并回收线程，不能让旧 heartbeat 泄漏到下一次 execution。

import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from loop_agent.runtime.core import AgentRuntimeError, SafeLogger, safe_subprocess_environment


BASE_DIR = Path(__file__).resolve().parents[3]
LOOPCTL = BASE_DIR / "scripts" / "loopctl.py"


class SubprocessLoopController:
    """Invoke the authoritative SQLite control plane through UTF-8 JSON CLI.

    The runtime intentionally does not import or mutate database functions
    directly. This adapter keeps claim/heartbeat/finish behind the same
    validations used by Codex and Dashboard callers.
    """

    def __init__(
        self, database: Path | None = None, timeout_seconds: float = 30
    ) -> None:
        self.database = database
        self.timeout_seconds = timeout_seconds
        self.claim_count = 0

    def _invoke(
        self,
        arguments: list[str],
        input_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        command = [sys.executable, str(LOOPCTL)]
        if self.database is not None:
            command.extend(["--db", str(self.database.resolve())])
        command.extend(arguments)
        environment = safe_subprocess_environment()
        environment["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            command,
            cwd=BASE_DIR,
            input=(
                None
                if input_payload is None
                else json.dumps(input_payload, ensure_ascii=False)
            ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise AgentRuntimeError(
                f"loop controller failed ({completed.returncode})"
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise AgentRuntimeError("loop controller returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise AgentRuntimeError("loop controller returned a non-object")
        return payload

    def claim(
        self,
        execution_id: str,
        runtime_environment: str,
        capability_level: str,
        provider_id: str | None = None,
    ) -> dict[str, Any]:
        """Claim exactly once per runtime process."""
        if self.claim_count:
            raise AgentRuntimeError("claim may only be called once per runtime instance")
        self.claim_count += 1
        arguments = [
            "claim",
            execution_id,
            "--runtime-environment",
            runtime_environment,
            "--capability-level",
            capability_level,
            "--execution-policy",
            "automatic",
        ]
        if provider_id is not None:
            arguments.extend(["--provider-id", provider_id])
        return self._invoke(arguments)

    def heartbeat(self, execution_id: str, task_id: str) -> dict[str, Any]:
        return self._invoke(["heartbeat", execution_id, task_id])

    def finish(
        self, execution_id: str, task_id: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        return self._invoke(
            ["finish", execution_id, task_id, "-"], input_payload=result
        )


class HeartbeatGuard:
    """Renew a claim on a background thread and surface failures synchronously."""

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
        self.thread = threading.Thread(
            target=self._run, name="agent-heartbeat", daemon=True
        )
        self.thread.start()
        return self

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval_seconds):
            try:
                self.beat()
            except Exception:  # pragma: no cover - wall-clock race
                self.stop_event.set()

    def beat(self) -> None:
        if self.error is not None:
            raise AgentRuntimeError("heartbeat failed") from self.error
        try:
            self.heartbeat()
        except Exception as error:
            self.error = error
            self.logger.event("heartbeat_failed", error=type(error).__name__)
            raise AgentRuntimeError("heartbeat failed") from error
        self.logger.event("heartbeat")

    def ensure_healthy(self) -> None:
        if self.error is not None:
            raise AgentRuntimeError("heartbeat failed") from self.error

    def __exit__(self, *_: Any) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=max(1.0, self.interval_seconds))
