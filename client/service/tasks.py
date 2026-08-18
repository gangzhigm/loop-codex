"""Dashboard 任务动作、恢复校验、健康投影和附件处理。

HTTP handler 解码请求后委托给本模块。修改操作仍通过 ``loopctl.py`` 执行，使 Dashboard
与 Operator 共用状态迁移和乐观并发规则。只有前置条件检查会直接读取 SQLite。
"""

from __future__ import annotations

# 中文排查：Dashboard 的归档、隔离恢复、附件图片解析和健康状态投影集中在这里。
# 动作失败先核对允许动作、row_version 和 execution 状态，再查看 loopctl 的结构化返回。
# 附件路径必须经过根目录约束，禁止把任意本机文件暴露为 Dashboard 图片。

import json
import re
import sqlite3
import subprocess
import sys
from http import HTTPStatus
from pathlib import Path

from loopdb import ARCHIVABLE_STATUSES, BASE_DIR, connect
from common.paths import HEALTH_STATE


TASK_ID_PATTERN = re.compile(r"[A-Z][A-Z0-9_-]*\Z")
IMAGE_CONTENT_TYPES = {
    ".avif": "image/avif",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class DashboardActionError(Exception):
    """公开任务动作失败及其 HTTP 响应状态。"""

    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


def run_loopctl(
    database_path: Path, arguments: list[str]
) -> dict[str, object]:
    """调用任务控制面，并严格解码一个 UTF-8 JSON 对象。"""
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(BASE_DIR / "control" / "loopctl.py"),
                "--db",
                str(database_path),
                *arguments,
            ],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="strict",
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as error:
        raise DashboardActionError(
            HTTPStatus.SERVICE_UNAVAILABLE, "任务状态服务执行失败"
        ) from error
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as error:
        raise DashboardActionError(
            HTTPStatus.SERVICE_UNAVAILABLE, "任务状态服务返回无效"
        ) from error
    if not isinstance(payload, dict):
        raise DashboardActionError(
            HTTPStatus.SERVICE_UNAVAILABLE, "任务状态服务返回无效"
        )
    if completed.returncode != 0 or payload.get("outcome") == "ERROR":
        message = payload.get("message")
        raise DashboardActionError(
            HTTPStatus.CONFLICT,
            str(message)
            if isinstance(message, str) and message
            else "任务状态已变化，请刷新后重试",
        )
    return payload


def archive_dashboard_task(
    database_path: Path,
    task_id: object,
    action: object,
    row_version: object,
) -> dict[str, object]:
    """必要时先确认成功任务，然后执行归档。"""
    if not isinstance(task_id, str) or TASK_ID_PATTERN.fullmatch(task_id) is None:
        raise DashboardActionError(HTTPStatus.BAD_REQUEST, "task_id 无效")
    if action != "archive":
        raise DashboardActionError(HTTPStatus.BAD_REQUEST, "action 仅支持 archive")
    if (
        isinstance(row_version, bool)
        or not isinstance(row_version, int)
        or row_version < 1
    ):
        raise DashboardActionError(HTTPStatus.BAD_REQUEST, "row_version 无效")

    database = connect(database_path)
    try:
        task = database.execute(
            "SELECT status, archived_at, row_version FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
    finally:
        database.close()
    if task is None:
        raise DashboardActionError(HTTPStatus.NOT_FOUND, "任务不存在")
    if task["row_version"] != row_version:
        raise DashboardActionError(
            HTTPStatus.CONFLICT, "任务状态已变化，请刷新后重试"
        )
    if task["archived_at"] is not None:
        raise DashboardActionError(
            HTTPStatus.CONFLICT, "任务已经归档，请刷新列表"
        )

    confirmed = False
    expected_row_version = row_version
    if task["status"] == "SUCCEEDED":
        confirmation = run_loopctl(
            database_path,
            [
                "confirm",
                task_id,
                "--reason",
                "Dashboard 人工确认并归档。",
                "--expected-row-version",
                str(expected_row_version),
            ],
        )
        expected_row_version = int(confirmation["row_version"])
        confirmed = True
    elif task["status"] not in ARCHIVABLE_STATUSES:
        raise DashboardActionError(
            HTTPStatus.CONFLICT, "当前任务状态不允许归档，请刷新后重试"
        )

    archived = run_loopctl(
        database_path,
        [
            "archive",
            task_id,
            "--reason",
            "Dashboard 人工归档任务。",
            "--expected-row-version",
            str(expected_row_version),
        ],
    )
    return {"ok": True, "confirmed": confirmed, **archived}


def runtime_health(
    health_state_path: Path = HEALTH_STATE,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """读取 UTF-8 健康快照，生成服务列表和近期事件列表。"""
    if not health_state_path.exists():
        return [], []
    try:
        value = json.loads(health_state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], []
    service = {
        key: value.get(key)
        for key in (
            "component",
            "status",
            "pid",
            "checked_at",
            "consecutive_failures",
            "message",
        )
    }
    services = [service] if service.get("component") else []
    monitors = value.get("monitors")
    if isinstance(monitors, dict):
        services.extend(
            item for item in monitors.values()
            if isinstance(item, dict) and isinstance(item.get("component"), str)
        )
        runners = monitors.get("runners")
        if isinstance(runners, dict) and isinstance(runners.get("instances"), list):
            services.extend(
                item for item in runners["instances"]
                if isinstance(item, dict) and isinstance(item.get("component"), str)
            )
    events = [
        item for item in (value.get("events") or []) if isinstance(item, dict)
    ][:12]
    return services, events


def resolve_attachment_image(
    database: sqlite3.Connection,
    task_id: str,
    attachment_path: str,
    base_dir: Path = BASE_DIR,
) -> tuple[Path, str]:
    """解析已登记的任务图片，并阻止附件路径越界。"""
    registered = database.execute(
        "SELECT 1 FROM task_attachments WHERE task_id=? AND path=?",
        (task_id, attachment_path),
    ).fetchone()
    if registered is None:
        raise FileNotFoundError("attachment not found")

    task_root = (base_dir / "data" / "assets" / task_id).resolve()
    image_path = (base_dir / attachment_path).resolve()
    if not image_path.is_relative_to(task_root):
        raise PermissionError(
            "attachment path is outside the task asset directory"
        )

    content_type = IMAGE_CONTENT_TYPES.get(image_path.suffix.lower())
    if content_type is None:
        raise ValueError("attachment is not a supported image")
    if not image_path.is_file():
        raise FileNotFoundError("attachment file not found")
    return image_path, content_type
