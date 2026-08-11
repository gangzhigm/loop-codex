"""Project registry parsing and hierarchical scope-lock rules.

A scope is normalized against the configured project registry before it can be
stored or locked. Comparisons are case-insensitive and operate on canonical
scope keys, which keeps file, module, project, and external conflicts
predictable across all execution environments.
"""

from __future__ import annotations

# 中文排查：项目清单解析、scope 规范化、锁键生成和冲突判定集中在这里。
# 冲突异常先比较规范化后的 scope_key，再核对 file/module/project 锁模式和项目大小写。
# 路径必须落在登记项目内，并拒绝越级、符号链接及禁止根目录，不能用字符串前缀代替解析。

import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from loop_agent.configuration import load_initialization_config
from loop_agent.constants import FORBIDDEN_SCOPE_ROOTS, LOCK_MODES
from loop_agent.errors import LoopError


def parse_project_registry(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    projects: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = re.match(r"^\|\s*`([^`]+)`\s*\|\s*(.*?)\s*\|\s*$", line)
        if not match or match.group(1) == "文件夹名":
            continue
        relative = match.group(1).replace("\\", "/").strip("/")
        projects.append(
            {
                "path": relative,
                "description": match.group(2).strip(),
                "exists_on_disk": int((path.parent / Path(relative)).exists()),
            }
        )
    if not projects:
        raise LoopError(f"项目清单没有可解析项目: {path}")
    return projects


def configured_projects(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    value = config or load_initialization_config()
    return parse_project_registry(Path(value["workspace"]["project_registry"]).resolve())


def _normalized_path_parts(value: str, field: str) -> list[str]:
    if not isinstance(value, str):
        raise LoopError(f"{field} 必须是字符串")
    normalized = value.replace("\\", "/").strip()
    if (
        not normalized
        or "\x00" in normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
    ):
        raise LoopError(f"不安全的 {field}: {value}")
    parts: list[str] = []
    for part in normalized.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise LoopError(f"不安全的 {field}: {value}")
        parts.append(part)
    if not parts:
        raise LoopError(f"不安全的 {field}: {value}")
    return parts


def normalize_scope(
    scope: str,
    lock_mode: str,
    project_paths: Iterable[str] | None = None,
) -> dict[str, str]:
    """Normalize a Planner scope and derive a case-insensitive hierarchical lock key."""
    if lock_mode not in LOCK_MODES:
        raise LoopError(f"lock_mode 无效: {lock_mode}")
    parts = _normalized_path_parts(scope, "scope")
    forbidden = {item.casefold() for item in FORBIDDEN_SCOPE_ROOTS}
    if parts[0].casefold() in forbidden or parts[0].upper().startswith("OSS:"):
        raise LoopError(f"scope 必须位于登记项目内: {scope}")
    paths = list(project_paths) if project_paths is not None else [item["path"] for item in configured_projects()]
    matches: list[tuple[list[str], list[str]]] = []
    folded_parts = [part.casefold() for part in parts]
    for raw_project in paths:
        project_parts = _normalized_path_parts(str(raw_project), "项目路径")
        folded_project = [part.casefold() for part in project_parts]
        if folded_parts[:len(folded_project)] == folded_project:
            matches.append((project_parts, folded_project))
    if not matches:
        raise LoopError(f"scope 未匹配项目清单: {scope}")
    project_parts, folded_project = max(matches, key=lambda item: len(item[0]))
    relative_parts = parts[len(project_parts):]
    if lock_mode in {"file", "module"} and not relative_parts:
        raise LoopError(f"{lock_mode} scope 必须指向项目内路径: {scope}")
    project = "/".join(project_parts)
    relative = "/".join(relative_parts)
    canonical_scope = project + (f"/{relative}" if relative else "")
    project_key = "/".join(folded_project)
    relative_key = "/".join(part.casefold() for part in relative_parts)
    if lock_mode == "project":
        scope_key = f"project:{project_key}"
    else:
        scope_key = f"{lock_mode}:{project_key}::{relative_key}"
    return {
        "scope": canonical_scope,
        "scope_key": scope_key,
        "project": project,
        "project_key": project_key,
        "relative": relative,
    }


def resolve_scope_key(
    scope: str,
    project_paths: Iterable[str] | None = None,
    lock_mode: str = "project",
) -> str:
    normalized = scope.replace("\\", "/").strip()
    if lock_mode == "project" and normalized.upper().startswith("OSS:"):
        if ".." in normalized.split("/"):
            raise LoopError(f"不安全的 scope: {scope}")
        return f"external:{normalized}"
    return normalize_scope(scope, lock_mode, project_paths)["scope_key"]


def parse_scope_key(scope_key: str) -> tuple[str, str, tuple[str, ...]]:
    if scope_key.startswith("external:"):
        return "external", scope_key.removeprefix("external:").casefold(), ()
    match = re.fullmatch(r"(file|module):(.+?)::(.+)", scope_key)
    if match:
        return match.group(1), match.group(2).casefold(), tuple(match.group(3).casefold().split("/"))
    if scope_key.startswith("project:"):
        return "project", scope_key.removeprefix("project:").casefold(), ()
    raise LoopError(f"scope_key 无效: {scope_key}")


def scope_keys_conflict(left: str, right: str) -> bool:
    left_mode, left_project, left_parts = parse_scope_key(left)
    right_mode, right_project, right_parts = parse_scope_key(right)
    if "external" in {left_mode, right_mode}:
        return left.casefold() == right.casefold()
    if left_project != right_project:
        return False
    if "project" in {left_mode, right_mode}:
        return True
    if left_mode == right_mode == "file":
        return left_parts == right_parts
    if left_mode == "module" and right_mode == "module":
        shorter = min(len(left_parts), len(right_parts))
        return left_parts[:shorter] == right_parts[:shorter]
    if left_mode == "module":
        return right_parts[:len(left_parts)] == left_parts
    if right_mode == "module":
        return left_parts[:len(right_parts)] == right_parts
    return False


def scope_conflicts_for_keys(
    database: sqlite3.Connection,
    scope_keys: Iterable[str],
    *,
    exclude_execution_id: str | None = None,
    exclude_task_id: str | None = None,
) -> list[dict[str, Any]]:
    requested = sorted(set(scope_keys))
    lock_columns = {row[1] for row in database.execute("PRAGMA table_info(scope_locks)")}
    status_projection = "status" if "status" in lock_columns else "'ACTIVE' AS status"
    locks = database.execute(
        f"SELECT scope_key, task_id, execution_id, {status_projection} FROM scope_locks ORDER BY scope_key"
    ).fetchall()
    conflicts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for requested_key in requested:
        for lock in locks:
            if exclude_execution_id is not None and lock["execution_id"] == exclude_execution_id:
                continue
            if exclude_task_id is not None and lock["task_id"] == exclude_task_id:
                continue
            if not scope_keys_conflict(requested_key, lock["scope_key"]):
                continue
            identity = (requested_key, lock["scope_key"], lock["execution_id"])
            if identity in seen:
                continue
            seen.add(identity)
            conflicts.append({
                "requested_scope_key": requested_key,
                "scope_key": lock["scope_key"],
                "blocker_task_id": lock["task_id"],
                "blocker_execution_id": lock["execution_id"],
                "blocker_lock_status": lock["status"],
            })
    return conflicts



