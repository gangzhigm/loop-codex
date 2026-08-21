"""不调用模型的确定性 Planner 验证 Worker。"""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = REPOSITORY_ROOT / "control"
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from common.files import write_json_atomic
from loopdb import CONFIG_PATH, DEFAULT_DB, load_initialization_config, now_shanghai
from runner.validation import ValidationWorkerSettings, is_validation_task


class VerificationWorkerError(RuntimeError):
    """验证 Worker 的受控流程失败。"""


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Local Agent Loop deterministic validation worker")
    value.add_argument("--db", default=str(DEFAULT_DB))
    value.add_argument("--config", default=str(CONFIG_PATH))
    value.add_argument("--execution-id", required=True)
    value.add_argument("--task-id", required=True)
    return value


def _run_loopctl(
    database_path: Path,
    command: list[str],
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    completed = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-B",
            str(CONTROL_ROOT / "loopctl.py"),
            "--db",
            str(database_path),
            *command,
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        input=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise VerificationWorkerError("验证 Worker 未收到受控命令的 JSON 结果") from error
    if completed.returncode != 0 or not isinstance(result, dict):
        raise VerificationWorkerError(str(result.get("message") or "验证 Worker 受控命令失败"))
    return result


def _artifact_path(settings: ValidationWorkerSettings, task_id: str, execution_id: str) -> Path:
    if any(value in {"", ".", ".."} or "/" in value or "\\" in value for value in (task_id, execution_id)):
        raise VerificationWorkerError("验证任务标识不安全")
    return settings.artifact_directory / f"{task_id}-{execution_id}.json"


def _artifact_label(artifact: Path) -> str:
    """项目内产物使用相对路径；测试目录仅用于安全诊断。"""
    try:
        return str(artifact.relative_to(REPOSITORY_ROOT))
    except ValueError:
        return str(artifact)


def run_validation_worker(args: argparse.Namespace) -> dict[str, Any]:
    args.validation_stage = "load_config"
    config = load_initialization_config(Path(args.config).resolve())
    settings = ValidationWorkerSettings.from_config(config)
    if not settings.enabled:
        raise VerificationWorkerError("验证 Worker 未启用")
    database_path = Path(args.db).resolve()
    args.validation_stage = "claim"
    claimed = _run_loopctl(
        database_path,
        [
            "preflight-claim",
            args.execution_id,
            "--task-id",
            args.task_id,
            "--runtime-environment",
            "self_hosted_agent",
            "--sandbox",
            "read-only",
        ],
    )
    if claimed.get("outcome") != "CLAIMED":
        return {"event": "runner.validation_worker.no_task", **claimed}
    task = claimed.get("task")
    definition = task.get("operator_definition") if isinstance(task, dict) else None
    if not isinstance(definition, dict) or not is_validation_task(
        args.task_id, definition.get("description"), settings
    ):
        raise VerificationWorkerError("验证 Worker 拒绝未标记任务")
    args.validation_stage = "write_prepared_artifact"
    artifact = _artifact_path(settings, args.task_id, args.execution_id)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        artifact,
        {
            "kind": "runner_validation_planner",
            "stage": "PREPARED",
            "task_id": args.task_id,
            "execution_id": args.execution_id,
            "prepared_at": now_shanghai(),
            "ready_after_seconds": settings.planner_ready_delay_seconds,
            "title": task.get("title"),
        },
    )
    stop_event = threading.Event()

    def request_stop(_signal: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, request_stop)
    if stop_event.wait(settings.planner_ready_delay_seconds):
        args.validation_stage = "publish_needs_review"
        review = _run_loopctl(
            database_path,
            [
                "preflight-needs-review",
                args.execution_id,
                args.task_id,
                "--expected-row-version",
                str(task["row_version"]),
            ],
            payload={
                "summary": "验证 Worker 在发布 READY 前收到停止请求。",
                "question": "是否重新运行此验证任务？",
                "options": ["重新排队", "取消验证"],
                "split_suggestions": [],
                "evidence": [f"验证检查文件：{_artifact_label(artifact)}"],
            },
        )
        return {
            "event": "runner.validation_worker.stopped",
            "task_id": args.task_id,
            "execution_id": args.execution_id,
            "result": review,
        }
    scope_hint = definition.get("scope_hint")
    capability = definition.get("estimated_capability_level")
    if not isinstance(scope_hint, list) or not scope_hint or not isinstance(capability, str):
        raise VerificationWorkerError("验证任务缺少 scope_hint 或预估能力等级")
    report = {
        "summary": "验证 Worker 已按延迟发布 READY，等待 Dispatcher 排队。",
        "capability_level": capability,
        "scope": scope_hint,
        "lock_mode": "project",
        "technical_acceptance": ["验证 Worker 已走完 Planner 领取和 READY 写回协议。"],
        "evidence": [f"验证检查文件：{_artifact_label(artifact)}"],
    }
    args.validation_stage = "publish_ready"
    ready = _run_loopctl(
        database_path,
        [
            "preflight-ready",
            args.execution_id,
            args.task_id,
            "--expected-row-version",
            str(task["row_version"]),
        ],
        payload=report,
    )
    args.validation_stage = "write_ready_artifact"
    write_json_atomic(
        artifact,
        {
            "kind": "runner_validation_planner",
            "stage": "READY",
            "task_id": args.task_id,
            "execution_id": args.execution_id,
            "prepared_at": now_shanghai(),
            "ready_at": now_shanghai(),
            "ready_result": ready,
        },
    )
    return {
        "event": "runner.validation_worker.ready",
        "task_id": args.task_id,
        "execution_id": args.execution_id,
        "artifact": _artifact_label(artifact),
        "result": ready,
    }


def main() -> None:
    args = parser().parse_args()
    try:
        print(json.dumps(run_validation_worker(args), ensure_ascii=False), flush=True)
    except Exception as error:
        print(
            json.dumps(
                {
                    "event": "runner.validation_worker.failed",
                    "task_id": args.task_id,
                    "execution_id": args.execution_id,
                    "stage": getattr(args, "validation_stage", "startup"),
                    "error": type(error).__name__,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
