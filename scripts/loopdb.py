from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = BASE_DIR / "data" / "loop-agent.sqlite3"
SCHEMA_PATH = BASE_DIR / "schemas" / "loop-agent.sql"
CONFIG_PATH = BASE_DIR / "config" / "initialization.json"
SCHEMA_VERSION = "3.7.0"
SCHEMA_USER_VERSION = 30700
PREFLIGHT_SCHEMA_USER_VERSION = 30600
DIAGNOSTIC_SCHEMA_USER_VERSION = 30500
RECOVERY_SCHEMA_USER_VERSION = 30400
PROFILE_ROUTING_SCHEMA_USER_VERSION = 30300
ROUTING_SCHEMA_USER_VERSION = 30200
ARCHIVE_SCHEMA_USER_VERSION = 30100
LEGACY_SCHEMA_USER_VERSION = 30000
SHANGHAI = timezone(timedelta(hours=8))
FINAL_EXECUTION_STATUSES = {"SUCCEEDED", "FAILED", "WAITING_HUMAN"}
DEPENDENCY_COMPLETE_STATUSES = {"SUCCEEDED", "CONFIRMED"}
ARCHIVABLE_STATUSES = {"CONFIRMED", "FAILED", "CANCELLED"}
PRIORITIES = ("blocker", "critical", "high", "medium", "low")
EXECUTION_PROFILES = ("routine", "standard", "advanced", "deep", "complex", "exceptional")
CAPABILITY_LEVELS = ("L1", "L2", "L3", "L4", "L5")
PREFLIGHT_STATUSES = ("UNINSPECTED", "INSPECTING", "READY", "FAILED")
LOCK_MODES = ("file", "module", "project")
EXECUTION_POLICIES = ("automatic", "manual")
CANONICAL_RUNTIME_ENVIRONMENTS = ("codex_automation", "codex_cli", "self_hosted_agent")
LEGACY_RUNTIME_ENVIRONMENTS = ("codex_automation", "codex_cli", "deepseek")
# Remove these input aliases after every launcher uses capability arguments and the compatibility window closes.
RUNTIME_ENVIRONMENTS = CANONICAL_RUNTIME_ENVIRONMENTS + ("deepseek",)
CLAIM_RUNTIME_ENVIRONMENTS = RUNTIME_ENVIRONMENTS
LEGACY_PROFILE_TO_CAPABILITY = {
    "routine": "L1", "standard": "L2", "advanced": "L3",
    "deep": "L4", "complex": "L5", "exceptional": "L5",
}
FORBIDDEN_SCOPE_ROOTS = {"$CODEX_HOME", ".reasonix", ".env"}
RESULT_DIAGNOSTIC_CATEGORIES = {
    "authentication", "connection", "empty_or_malformed_response", "final_schema",
    "invalid_final_json", "invalid_tool_call", "local_protocol", "rate_limited",
    "request_invalid", "request_timeout", "server_error", "truncated_response",
    "unsupported_finish_reason",
}
RESULT_DIAGNOSTIC_FINISH_REASONS = {
    "length", "content_filter", "insufficient_system_resource", "stop", "tool_calls",
}
RESULT_DIAGNOSTIC_FIELD_NAMES = (
    "status", "summary", "verification", "completed", "error", "question", "options",
    "result", "message", "output",
)
RESULT_DIAGNOSTIC_TYPE_TAGS = {
    "array", "boolean", "null", "number", "object", "string", "unavailable",
}
RESULT_DIAGNOSTIC_PARSE_STATES = {"invalid_json", "parsed", "unavailable"}
TRANSIENT_RESULT_DIAGNOSTIC_CATEGORIES = {
    "connection", "rate_limited", "request_timeout", "server_error",
}
ALLOWED_TABLES = {
    "tasks",
    "task_dependencies",
    "task_scopes",
    "task_acceptance",
    "task_technical_acceptance",
    "task_preflight_evidence",
    "task_completed_items",
    "task_verifications",
    "task_attachments",
    "task_history",
    "executions",
    "preflight_executions",
    "scope_locks",
    "task_conflicts",
}

TASKS_TABLE_SQL = """
CREATE TABLE tasks_new (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN (
    'DRAFT', 'NEEDS_REVIEW', 'PENDING', 'RUNNING', 'WAITING_CONFLICT', 'WAITING_HUMAN',
    'SUCCEEDED', 'CONFIRMED', 'FAILED', 'CANCELLED'
  )),
  priority TEXT NOT NULL CHECK (priority IN ('blocker', 'critical', 'high', 'medium', 'low')),
  estimated_capability_level TEXT CHECK (estimated_capability_level IS NULL OR estimated_capability_level IN (
    'L1', 'L2', 'L3', 'L4', 'L5'
  )),
  capability_level TEXT CHECK (capability_level IS NULL OR capability_level IN (
    'L1', 'L2', 'L3', 'L4', 'L5'
  )),
  runtime_environment TEXT NOT NULL CHECK (runtime_environment IN (
    'codex_automation', 'codex_cli', 'self_hosted_agent'
  )),
  provider_id TEXT,
  execution_policy TEXT NOT NULL DEFAULT 'automatic' CHECK (execution_policy IN (
    'automatic', 'manual'
  )),
  preflight_status TEXT NOT NULL DEFAULT 'UNINSPECTED' CHECK (preflight_status IN (
    'UNINSPECTED', 'INSPECTING', 'READY', 'FAILED'
  )),
  preflight_execution_id TEXT,
  preflight_started_at TEXT,
  preflight_completed_at TEXT,
  preflight_failure TEXT,
  scope_hint_json TEXT NOT NULL DEFAULT '[]',
  lock_mode TEXT CHECK (lock_mode IS NULL OR lock_mode IN ('file', 'module', 'project')),
  split_suggestions_json TEXT NOT NULL DEFAULT '[]',
  assigned_agent TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  updated_at TEXT NOT NULL,
  heartbeat_at TEXT,
  completed_at TEXT,
  archived_at TEXT,
  attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
  progress_percent INTEGER NOT NULL DEFAULT 0 CHECK (progress_percent BETWEEN 0 AND 100),
  progress_summary TEXT NOT NULL DEFAULT '',
  progress_next_step TEXT,
  result_summary TEXT,
  result_error TEXT,
  result_diagnostic_json TEXT,
  human_required INTEGER NOT NULL DEFAULT 0 CHECK (human_required IN (0, 1)),
  human_question TEXT,
  human_options_json TEXT NOT NULL DEFAULT '[]',
  human_requested_at TEXT,
  human_responded_at TEXT,
  human_response TEXT,
  row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version >= 1),
  CHECK (
    (runtime_environment = 'self_hosted_agent' AND provider_id IS NOT NULL AND length(trim(provider_id)) > 0)
    OR (runtime_environment <> 'self_hosted_agent' AND provider_id IS NULL)
  )
)
"""


def normalize_result_diagnostic(raw: Any) -> dict[str, Any] | None:
    """Validate and canonicalize value-free result diagnostics."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LoopError("result diagnostic 必须是对象")
    allowed = {
        "category", "http_status", "retryable", "retry_exhausted", "finish_reason",
        "agent_attempt", "model_step", "final_shape",
    }
    if set(raw) - allowed:
        raise LoopError("result diagnostic 包含未知字段")
    category = raw.get("category")
    if category not in RESULT_DIAGNOSTIC_CATEGORIES:
        raise LoopError("result diagnostic category 无效")
    http_status = raw.get("http_status")
    if http_status is not None and (
        not isinstance(http_status, int) or isinstance(http_status, bool) or not 100 <= http_status <= 599
    ):
        raise LoopError("result diagnostic HTTP status 无效")
    retryable = raw.get("retryable")
    retry_exhausted = raw.get("retry_exhausted")
    if not isinstance(retryable, bool) or not isinstance(retry_exhausted, bool):
        raise LoopError("result diagnostic retry 字段无效")
    if retryable and category not in TRANSIENT_RESULT_DIAGNOSTIC_CATEGORIES:
        raise LoopError("result diagnostic category 不允许重试")
    if retry_exhausted and not retryable:
        raise LoopError("result diagnostic retry_exhausted 无效")
    finish_reason = raw.get("finish_reason")
    if finish_reason is not None and finish_reason not in RESULT_DIAGNOSTIC_FINISH_REASONS:
        raise LoopError("result diagnostic finish_reason 无效")
    attempts: dict[str, int | None] = {}
    for key in ("agent_attempt", "model_step"):
        value = raw.get(key)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 10_000
        ):
            raise LoopError(f"result diagnostic {key} 无效")
        attempts[key] = value
    final_shape = _normalize_final_shape(raw.get("final_shape"))
    if final_shape is not None and finish_reason != "stop":
        raise LoopError("result diagnostic final_shape 需要 stop")
    if final_shape is not None and category not in {"final_schema", "invalid_final_json"}:
        raise LoopError("result diagnostic category 不允许 final_shape")
    result: dict[str, Any] = {
        "category": category,
        "http_status": http_status,
        "retryable": retryable,
        "retry_exhausted": retry_exhausted,
        "finish_reason": finish_reason,
        **attempts,
    }
    if final_shape is not None:
        result["final_shape"] = final_shape
    return result


def _normalize_final_shape(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {
        "finish_reason", "content_length", "json_parse_state", "top_level_type",
        "allowed_fields", "unknown_field_count", "unknown_fields_present",
    }:
        raise LoopError("result diagnostic final_shape 无效")
    if raw.get("finish_reason") != "stop":
        raise LoopError("result diagnostic shape finish_reason 无效")
    content_length = raw.get("content_length")
    unknown_count = raw.get("unknown_field_count")
    unknown_present = raw.get("unknown_fields_present")
    if not isinstance(content_length, int) or isinstance(content_length, bool) or not 0 <= content_length <= 10_000_000:
        raise LoopError("result diagnostic content_length 无效")
    if not isinstance(unknown_count, int) or isinstance(unknown_count, bool) or not 0 <= unknown_count <= 10_000:
        raise LoopError("result diagnostic unknown_field_count 无效")
    if not isinstance(unknown_present, bool) or unknown_present != (unknown_count > 0):
        raise LoopError("result diagnostic unknown_fields_present 无效")
    parse_state = raw.get("json_parse_state")
    top_level_type = raw.get("top_level_type")
    if parse_state not in RESULT_DIAGNOSTIC_PARSE_STATES or top_level_type not in RESULT_DIAGNOSTIC_TYPE_TAGS:
        raise LoopError("result diagnostic shape 类型无效")
    fields = raw.get("allowed_fields")
    if not isinstance(fields, dict) or set(fields) != set(RESULT_DIAGNOSTIC_FIELD_NAMES):
        raise LoopError("result diagnostic allowed_fields 无效")
    normalized_fields: dict[str, dict[str, Any]] = {}
    for name in RESULT_DIAGNOSTIC_FIELD_NAMES:
        field = fields[name]
        if not isinstance(field, dict) or set(field) != {"present", "type"}:
            raise LoopError("result diagnostic field metadata 无效")
        present = field.get("present")
        value_type = field.get("type")
        if not isinstance(present, bool) or value_type not in RESULT_DIAGNOSTIC_TYPE_TAGS:
            raise LoopError("result diagnostic field type 无效")
        if present == (value_type == "unavailable"):
            raise LoopError("result diagnostic field presence/type 不一致")
        normalized_fields[name] = {"present": present, "type": value_type}
    if parse_state == "parsed" and top_level_type == "unavailable":
        raise LoopError("result diagnostic parsed top-level type 无效")
    if parse_state != "parsed" and (
        top_level_type != "unavailable" or unknown_count != 0
        or any(field["present"] for field in normalized_fields.values())
    ):
        raise LoopError("result diagnostic unparsed shape 无效")
    return {
        "finish_reason": "stop",
        "content_length": content_length,
        "json_parse_state": parse_state,
        "top_level_type": top_level_type,
        "allowed_fields": normalized_fields,
        "unknown_field_count": unknown_count,
        "unknown_fields_present": unknown_present,
    }

EXECUTIONS_TABLE_SQL = """
CREATE TABLE executions_new (
  execution_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK (status IN (
    'RUNNING', 'FINISHED', 'EXPIRED', 'STALLED', 'TIMED_OUT'
  )),
  started_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  lease_expires_at TEXT NOT NULL,
  finished_at TEXT,
  outcome TEXT,
  execution_kind TEXT NOT NULL DEFAULT 'WORKER' CHECK (execution_kind = 'WORKER'),
  runtime_environment TEXT NOT NULL CHECK (runtime_environment IN (
    'codex_automation', 'codex_cli', 'self_hosted_agent'
  )),
  provider_id TEXT,
  capability_level TEXT NOT NULL CHECK (capability_level IN ('L1', 'L2', 'L3', 'L4', 'L5')),
  execution_policy TEXT NOT NULL CHECK (execution_policy IN ('automatic', 'manual')),
  model TEXT NOT NULL,
  reasoning TEXT NOT NULL CHECK (reasoning IN ('low', 'medium', 'high', 'xhigh')),
  attempt_timeout_seconds INTEGER NOT NULL CHECK (attempt_timeout_seconds > 0),
  max_retries INTEGER NOT NULL CHECK (max_retries >= 0),
  termination_reason TEXT,
  recovery_required INTEGER NOT NULL DEFAULT 0 CHECK (recovery_required IN (0, 1)),
  recovered_at TEXT,
  recovery_action TEXT CHECK (recovery_action IS NULL OR recovery_action IN (
    'requeue', 'failed', 'wait'
  )),
  CHECK (
    (runtime_environment = 'self_hosted_agent' AND provider_id IS NOT NULL AND length(trim(provider_id)) > 0)
    OR (runtime_environment <> 'self_hosted_agent' AND provider_id IS NULL)
  )
)
"""

SCOPE_LOCKS_TABLE_SQL = """
CREATE TABLE scope_locks_new (
  scope_key TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  execution_id TEXT NOT NULL REFERENCES executions(execution_id) ON DELETE CASCADE,
  acquired_at TEXT NOT NULL,
  lease_expires_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'QUARANTINED')),
  quarantined_at TEXT,
  quarantine_reason TEXT,
  CHECK (
    (status = 'ACTIVE' AND quarantined_at IS NULL AND quarantine_reason IS NULL)
    OR (status = 'QUARANTINED' AND quarantined_at IS NOT NULL AND length(trim(quarantine_reason)) > 0)
  )
)
"""

PREFLIGHT_EXECUTIONS_TABLE_SQL = """
CREATE TABLE preflight_executions (
  execution_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  execution_kind TEXT NOT NULL DEFAULT 'PLANNER' CHECK (execution_kind = 'PLANNER'),
  status TEXT NOT NULL CHECK (status IN ('INSPECTING', 'FINISHED', 'FAILED', 'TIMED_OUT')),
  started_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  lease_expires_at TEXT NOT NULL,
  attempt_deadline_at TEXT NOT NULL,
  finished_at TEXT,
  outcome TEXT CHECK (outcome IS NULL OR outcome IN ('READY', 'NEEDS_REVIEW', 'FAILED', 'TIMED_OUT')),
  termination_reason TEXT,
  claimed_task_row_version INTEGER NOT NULL CHECK (claimed_task_row_version >= 1),
  recovered_at TEXT,
  recovery_action TEXT CHECK (recovery_action IS NULL OR recovery_action = 'requeue')
)
"""


class LoopError(RuntimeError):
    pass


def normalize_string_list(raw: Any, field: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(raw, list):
        raise LoopError(f"{field} 必须是数组")
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise LoopError(f"{field} 只能包含非空字符串")
        values.append(item.strip())
    if len(values) != len(set(values)):
        raise LoopError(f"{field} 不能包含重复项")
    if not allow_empty and not values:
        raise LoopError(f"{field} 不能为空")
    return values


def normalize_split_suggestions(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise LoopError("split_suggestions 必须是数组")
    suggestions: list[dict[str, Any]] = []
    for suggestion in raw:
        if not isinstance(suggestion, dict) or set(suggestion) != {"reason", "tasks"}:
            raise LoopError("split_suggestions 项必须只包含 reason 和 tasks")
        reason = suggestion.get("reason")
        tasks = suggestion.get("tasks")
        if not isinstance(reason, str) or not reason.strip() or not isinstance(tasks, list) or not tasks:
            raise LoopError("split_suggestions 的 reason 和 tasks 不能为空")
        normalized_tasks: list[dict[str, Any]] = []
        identifiers: set[str] = set()
        for proposed in tasks:
            required = {
                "id", "title", "description", "scope", "capability_level",
                "depends_on", "parallel_with",
            }
            if not isinstance(proposed, dict) or set(proposed) != required:
                raise LoopError("拆分子任务字段不完整")
            task_id = proposed.get("id")
            title = proposed.get("title")
            description = proposed.get("description")
            capability = proposed.get("capability_level")
            if not isinstance(task_id, str) or not re.fullmatch(r"[A-Z][A-Z0-9_-]*", task_id):
                raise LoopError("拆分子任务 id 无效")
            if task_id in identifiers:
                raise LoopError("拆分子任务 id 不能重复")
            identifiers.add(task_id)
            if not isinstance(title, str) or not title.strip():
                raise LoopError("拆分子任务 title 不能为空")
            if not isinstance(description, str) or not description.strip():
                raise LoopError("拆分子任务 description 不能为空")
            if capability not in CAPABILITY_LEVELS:
                raise LoopError("拆分子任务 capability_level 无效")
            normalized_tasks.append({
                "id": task_id,
                "title": title.strip(),
                "description": description.strip(),
                "scope": normalize_string_list(proposed.get("scope"), "拆分子任务 scope", allow_empty=False),
                "capability_level": capability,
                "depends_on": normalize_string_list(proposed.get("depends_on"), "拆分子任务 depends_on"),
                "parallel_with": normalize_string_list(proposed.get("parallel_with"), "拆分子任务 parallel_with"),
            })
        suggestions.append({"reason": reason.strip(), "tasks": normalized_tasks})
    return suggestions


def legacy_profile_for(capability_level: str, execution_policy: str = "automatic") -> str:
    if capability_level not in CAPABILITY_LEVELS:
        raise LoopError(f"任务能力等级无效: {capability_level}")
    if execution_policy not in EXECUTION_POLICIES:
        raise LoopError(f"执行策略无效: {execution_policy}")
    if capability_level == "L5":
        return "exceptional" if execution_policy == "manual" else "complex"
    return {"L1": "routine", "L2": "standard", "L3": "advanced", "L4": "deep"}[capability_level]


def normalize_execution_target(
    runtime_environment: str,
    provider_id: str | None = None,
) -> tuple[str, str | None]:
    if runtime_environment == "deepseek":
        if provider_id not in {None, "deepseek"}:
            raise LoopError("旧 deepseek 路由只能映射到 provider_id=deepseek")
        return "self_hosted_agent", "deepseek"
    if runtime_environment not in CANONICAL_RUNTIME_ENVIRONMENTS:
        raise LoopError(f"运行环境无效: {runtime_environment}")
    if runtime_environment == "self_hosted_agent":
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise LoopError("self_hosted_agent 任务必须提供 provider_id")
        return runtime_environment, provider_id.strip()
    if provider_id is not None:
        raise LoopError(f"{runtime_environment} 任务不得保存 provider_id")
    return runtime_environment, None


def resolve_execution_profile(
    runtime_environment: str,
    provider_id: str | None,
    capability_level: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value = config or load_initialization_config()
    runtime_environment, provider_id = normalize_execution_target(runtime_environment, provider_id)
    if capability_level not in CAPABILITY_LEVELS:
        raise LoopError(f"任务能力等级无效: {capability_level}")
    runtime_profiles = (value.get("execution_profiles") or {}).get(runtime_environment) or {}
    if runtime_environment == "self_hosted_agent":
        profile = ((runtime_profiles.get("providers") or {}).get(provider_id) or {}).get(
            "capabilities", {}
        ).get(capability_level)
    else:
        if runtime_profiles.get("provider_id") is not None:
            raise LoopError(f"{runtime_environment} execution profile 不得配置 provider_id")
        profile = (runtime_profiles.get("capabilities") or {}).get(capability_level)
    if not isinstance(profile, dict):
        raise LoopError(
            f"没有匹配的 execution profile: {runtime_environment}/{provider_id or '-'}/{capability_level}"
        )
    return {
        "runtime_environment": runtime_environment,
        "provider_id": provider_id,
        "capability_level": capability_level,
        "model": profile["model"],
        "reasoning": profile["reasoning"],
        "attempt_timeout_seconds": int(profile["attempt_timeout_seconds"]),
        "max_retries": int(profile["max_retries"]),
    }


def load_initialization_config(path: Path | str = CONFIG_PATH) -> dict[str, Any]:
    config_path = Path(path).resolve()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LoopError(f"初始化配置无效: {config_path}: {error}") from error
    workspace = config.get("workspace") or {}
    database = config.get("database") or {}
    prompts = config.get("prompts") or {}
    execution = config.get("task_execution") or {}
    planner = config.get("planner") or {}
    planner_boundary = planner.get("client_boundary") or {}
    planner_writeback = planner_boundary.get("writeback") or {}
    self_hosted_agent = config.get("self_hosted_agent") or {}
    deepseek = config.get("deepseek") or {}
    priority_policy = config.get("priority_policy") or {}
    dashboard = config.get("dashboard") or {}
    automations = config.get("automations") or {}
    planner_automation = automations.get("planner") or {}
    health = config.get("health") or {}
    platform_limits = execution.get("platform_max_active_executions") or {}
    profiles = automations.get("profiles") or {}
    runtime_environments = config.get("runtime_environments") or {}
    project_defaults = priority_policy.get("project_defaults") or {}
    valid_profile_config = set(profiles) == set(EXECUTION_PROFILES) and all(
        isinstance(profiles[profile], dict)
        and isinstance(profiles[profile].get("name"), str)
        and isinstance(profiles[profile].get("model"), str)
        and profiles[profile].get("reasoning_effort") in {"low", "medium", "high", "xhigh"}
        and isinstance(profiles[profile].get("scheduled"), bool)
        and (
            (profiles[profile]["scheduled"] and isinstance(profiles[profile].get("automation_id"), str)
             and isinstance(profiles[profile].get("offset_minutes"), int))
            or (not profiles[profile]["scheduled"] and profiles[profile].get("automation_id") is None
                and profiles[profile].get("offset_minutes") is None)
        )
        for profile in EXECUTION_PROFILES
    )
    valid_runtime_environment_config = set(runtime_environments) == set(CANONICAL_RUNTIME_ENVIRONMENTS) and all(
        isinstance(runtime_environments[environment], dict)
        and isinstance(runtime_environments[environment].get("name"), str)
        and bool(runtime_environments[environment]["name"].strip())
        and isinstance(runtime_environments[environment].get("entry"), dict)
        and runtime_environments[environment]["entry"].get("type") == environment
        and runtime_environments[environment]["entry"].get("claim_argument") == environment
        for environment in CANONICAL_RUNTIME_ENVIRONMENTS
    )
    execution_profiles = config.get("execution_profiles") or {}
    valid_profile_fields = lambda item: (
        isinstance(item, dict)
        and isinstance(item.get("model"), str) and bool(item["model"].strip())
        and item.get("reasoning") in {"low", "medium", "high", "xhigh"}
        and isinstance(item.get("attempt_timeout_seconds"), int)
        and item["attempt_timeout_seconds"] > 0
        and isinstance(item.get("max_retries"), int)
        and 0 <= item["max_retries"] <= 10
    )
    direct_profiles_valid = all(
        isinstance(execution_profiles.get(environment), dict)
        and execution_profiles[environment].get("provider_id") is None
        and set((execution_profiles[environment].get("capabilities") or {})) == set(CAPABILITY_LEVELS)
        and all(valid_profile_fields(item) for item in execution_profiles[environment]["capabilities"].values())
        for environment in ("codex_automation", "codex_cli")
    )
    self_hosted_profiles = execution_profiles.get("self_hosted_agent") or {}
    providers = self_hosted_profiles.get("providers") or {}
    self_hosted_profiles_valid = (
        isinstance(providers, dict) and bool(providers)
        and all(
            isinstance(provider, str) and bool(provider.strip())
            and isinstance(item, dict)
            and set((item.get("capabilities") or {})) == set(CAPABILITY_LEVELS)
            and all(valid_profile_fields(profile) for profile in item["capabilities"].values())
            for provider, item in providers.items()
        )
    )
    valid = (
        config.get("config_version") == "4.2.0"
        and workspace.get("timezone") == "Asia/Shanghai"
        and isinstance(workspace.get("name"), str)
        and isinstance(workspace.get("task_root"), str)
        and isinstance(workspace.get("project_registry"), str)
        and database.get("path") == "data/loop-agent.sqlite3"
        and database.get("schema_version") == SCHEMA_VERSION
        and prompts.get("operator") == "prompts/operator.md"
        and prompts.get("planner") == "prompts/planner.md"
        and prompts.get("worker") == "prompts/worker.md"
        and (BASE_DIR / prompts["operator"]).is_file()
        and (BASE_DIR / prompts["planner"]).is_file()
        and (BASE_DIR / prompts["worker"]).is_file()
        and isinstance(execution.get("heartbeat_interval_seconds"), int)
        and execution["heartbeat_interval_seconds"] >= 1
        and isinstance(execution.get("stalled_after_seconds"), int)
        and execution["stalled_after_seconds"] >= 1
        and isinstance(execution.get("task_lease_seconds"), int)
        and execution["task_lease_seconds"] >= 60
        and isinstance(execution.get("max_attempts"), int)
        and execution["max_attempts"] >= 1
        and isinstance(execution.get("global_max_active_executions"), int)
        and execution["global_max_active_executions"] >= 1
        and set(platform_limits) == set(CANONICAL_RUNTIME_ENVIRONMENTS)
        and all(
            isinstance(platform_limits[platform], int) and platform_limits[platform] >= 1
            for platform in CANONICAL_RUNTIME_ENVIRONMENTS
        )
        and execution.get("scope_conflict_mode") == "project"
        and isinstance(execution.get("require_human_approval_for"), list)
        and planner.get("execution_kind") == "PLANNER"
        and planner.get("default_runtime_environment") in CANONICAL_RUNTIME_ENVIRONMENTS
        and isinstance(planner.get("max_active_executions"), int)
        and planner["max_active_executions"] >= 1
        and isinstance(planner.get("heartbeat_interval_seconds"), int)
        and planner["heartbeat_interval_seconds"] >= 1
        and isinstance(planner.get("stalled_after_seconds"), int)
        and planner["stalled_after_seconds"] >= planner["heartbeat_interval_seconds"]
        and isinstance(planner.get("lease_seconds"), int)
        and planner["lease_seconds"] >= 60
        and isinstance(planner.get("attempt_timeout_seconds"), int)
        and planner["attempt_timeout_seconds"] >= planner["lease_seconds"]
        and planner_boundary.get("sandbox") == "read-only"
        and planner_boundary.get("approval_policy") == "never"
        and planner_boundary.get("network_access") is False
        and planner_boundary.get("default_tool_action") == "deny"
        and planner_boundary.get("source_access") == "read-only"
        and planner_writeback.get("transport") == "host_controlled_loopctl_stdin"
        and planner_writeback.get("controller") == str(BASE_DIR / "scripts" / "loopctl.py")
        and planner_writeback.get("allowed_commands") == [
            "preflight-claim", "preflight-heartbeat", "preflight-ready",
            "preflight-needs-review", "preflight-fail",
        ]
        and planner_writeback.get("direct_sql") is False
        and planner_writeback.get("report_files") is False
        and isinstance(self_hosted_agent.get("max_steps"), int)
        and 1 <= self_hosted_agent["max_steps"] <= 200
        and isinstance(self_hosted_agent.get("max_final_repairs"), int)
        and self_hosted_agent["max_final_repairs"] in {0, 1}
        and isinstance(self_hosted_agent.get("model_timeout_seconds"), (int, float))
        and self_hosted_agent["model_timeout_seconds"] > 0
        and isinstance(self_hosted_agent.get("tool_timeout_seconds"), (int, float))
        and self_hosted_agent["tool_timeout_seconds"] > 0
        and isinstance(self_hosted_agent.get("max_file_bytes"), int)
        and self_hosted_agent["max_file_bytes"] >= 1024
        and isinstance(self_hosted_agent.get("max_tool_output_chars"), int)
        and self_hosted_agent["max_tool_output_chars"] >= 1024
        and isinstance(deepseek.get("api_base_url"), str)
        and deepseek["api_base_url"].startswith("https://")
        and isinstance(deepseek.get("model"), str)
        and bool(deepseek["model"].strip())
        and isinstance(deepseek.get("timeout_seconds"), (int, float))
        and deepseek["timeout_seconds"] > 0
        and isinstance(deepseek.get("max_retries"), int)
        and 0 <= deepseek["max_retries"] <= 10
        and isinstance(deepseek.get("retry_backoff_seconds"), (int, float))
        and deepseek["retry_backoff_seconds"] >= 0
        and isinstance(deepseek.get("max_retry_backoff_seconds"), (int, float))
        and deepseek["max_retry_backoff_seconds"] >= deepseek["retry_backoff_seconds"]
        and isinstance(deepseek.get("api_key_environment_variable"), str)
        and bool(deepseek["api_key_environment_variable"].strip())
        and isinstance(deepseek.get("supported_execution_profiles"), list)
        and bool(deepseek["supported_execution_profiles"])
        and all(profile in EXECUTION_PROFILES for profile in deepseek["supported_execution_profiles"])
        and priority_policy.get("levels") == list(PRIORITIES)
        and all(priority in PRIORITIES for priority in project_defaults.values())
        and isinstance(dashboard.get("host"), str)
        and isinstance(dashboard.get("port"), int)
        and 1 <= dashboard["port"] <= 65535
        and isinstance(dashboard.get("poll_interval_ms"), int)
        and dashboard["poll_interval_ms"] >= 500
        and isinstance(automations.get("worker_interval_minutes"), int)
        and automations["worker_interval_minutes"] >= 1
        and isinstance(automations.get("entry_prompt_template"), str)
        and "{profile}" in automations["entry_prompt_template"]
        and automations.get("runtime_environment") == "codex_automation"
        and "codex_automation" in automations["entry_prompt_template"]
        and planner_automation.get("automation_id") == "loop-agent-planner"
        and planner_automation.get("name") == "Loop Agent Planner"
        and planner_automation.get("scheduled") is True
        and planner_automation.get("interval_minutes") == 5
        and planner_automation.get("model") == "gpt-5.6-terra"
        and planner_automation.get("reasoning_effort") == "high"
        and planner_automation.get("runtime_environment") == "codex_automation"
        and planner_automation.get("execution_kind") == "PLANNER"
        and planner_automation.get("sandbox") == "read-only"
        and planner_automation.get("approval_policy") == "never"
        and isinstance(planner_automation.get("entry_prompt"), str)
        and "prompts\\planner.md" in planner_automation["entry_prompt"]
        and "runtime_environment=codex_automation" in planner_automation["entry_prompt"]
        and "execution_kind=PLANNER" in planner_automation["entry_prompt"]
        and "sandbox=read-only" in planner_automation["entry_prompt"]
        and valid_profile_config
        and valid_runtime_environment_config
        and set(execution_profiles) == set(CANONICAL_RUNTIME_ENVIRONMENTS)
        and direct_profiles_valid
        and self_hosted_profiles_valid
        and health.get("scheduler") == "windows_task_scheduler"
        and isinstance(health.get("task_name"), str)
        and bool(health["task_name"].strip())
        and isinstance(health.get("interval_minutes"), int)
        and health["interval_minutes"] >= 1
        and isinstance(health.get("failure_threshold"), int)
        and health["failure_threshold"] >= 1
    )
    if not valid:
        raise LoopError(f"初始化配置字段或取值无效: {config_path}")
    return config


def now_shanghai() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="milliseconds")


def expires_at(seconds: int) -> str:
    return (datetime.now(SHANGHAI) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_load(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def load_result_diagnostic(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError) as error:
        raise LoopError("result diagnostic JSON 无效") from error
    normalized = normalize_result_diagnostic(parsed)
    if normalized is None or json_dump(normalized) != value:
        raise LoopError("result diagnostic 不是规范 JSON")
    return normalized


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(str(database_path), timeout=5.0, isolation_level=None)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys = ON")
    database.execute("PRAGMA journal_mode = WAL")
    database.execute("PRAGMA busy_timeout = 5000")
    database.execute("PRAGMA synchronous = NORMAL")
    return database


def initialize_schema(database: sqlite3.Connection) -> None:
    current = int(database.execute("PRAGMA user_version").fetchone()[0])
    if current not in {0, SCHEMA_USER_VERSION}:
        raise LoopError(
            f"数据库 Schema 不是当前版本: user_version={current}；请先运行 loopctl.py migrate"
        )
    database.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def _create_current_execution_indexes(database: sqlite3.Connection) -> None:
    database.execute("CREATE INDEX idx_executions_active ON executions(status, lease_expires_at)")
    database.execute(
        "CREATE UNIQUE INDEX idx_executions_one_active_task "
        "ON executions(task_id) WHERE status='RUNNING'"
    )


def _create_preflight_schema_objects(database: sqlite3.Connection) -> None:
    database.execute(
        "CREATE TABLE task_technical_acceptance ("
        "task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, "
        "ordinal INTEGER NOT NULL, text TEXT NOT NULL, PRIMARY KEY(task_id, ordinal))"
    )
    database.execute(
        "CREATE TABLE task_preflight_evidence ("
        "task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, "
        "ordinal INTEGER NOT NULL, text TEXT NOT NULL, PRIMARY KEY(task_id, ordinal))"
    )
    database.execute(PREFLIGHT_EXECUTIONS_TABLE_SQL)
    database.execute(
        "CREATE INDEX idx_preflight_executions_active "
        "ON preflight_executions(status, lease_expires_at, attempt_deadline_at)"
    )
    database.execute(
        "CREATE UNIQUE INDEX idx_preflight_one_active_task "
        "ON preflight_executions(task_id) WHERE status='INSPECTING'"
    )


def _migrate_preflight_schema(database: sqlite3.Connection, source_version: str = "3.6.0") -> dict[str, Any]:
    task_rows = [dict(row) for row in database.execute("SELECT * FROM tasks ORDER BY id").fetchall()]
    scopes = {
        row["task_id"]: [] for row in database.execute("SELECT DISTINCT task_id FROM task_scopes").fetchall()
    }
    for row in database.execute("SELECT task_id, scope FROM task_scopes ORDER BY task_id, ordinal"):
        scopes.setdefault(row["task_id"], []).append(row["scope"])
    active = int(database.execute("SELECT count(*) FROM executions WHERE status='RUNNING'").fetchone()[0])
    stamp = now_shanghai()
    old_drafts = 0
    ready = 0
    ready_ids: set[str] = set()
    database.execute("PRAGMA foreign_keys = OFF")
    try:
        transaction(database)
        database.execute(TASKS_TABLE_SQL)
        for row in task_rows:
            was_draft = row["status"] == "DRAFT"
            preflight_ready = not was_draft and bool(scopes.get(row["id"], []))
            status = "NEEDS_REVIEW" if was_draft else row["status"]
            preflight_status = "READY" if preflight_ready else "FAILED"
            failure = (
                "Schema 3.7.0 迁移：旧 DRAFT 的边界未经过 Planner 预检，需要人工补充后重新预检。"
                if was_draft else (
                    "Schema 3.7.0 迁移：历史终态缺少精确 scope，未声明为 READY。"
                    if not preflight_ready else None
                )
            )
            estimated = row.get("capability_level")
            capability = None if was_draft else row.get("capability_level")
            scope_hint = json_dump(scopes.get(row["id"], []))
            values = (
                row["id"], row["title"], row.get("description", ""), status, row["priority"],
                estimated, capability, row["runtime_environment"], row.get("provider_id"),
                row["execution_policy"], preflight_status, None, None,
                stamp if preflight_ready else None, failure, scope_hint,
                "project" if preflight_ready else None, "[]", row.get("assigned_agent"),
                row["created_at"], row.get("started_at"), stamp if was_draft else row["updated_at"],
                row.get("heartbeat_at"), row.get("completed_at"), row.get("archived_at"),
                row.get("attempt", 0), row.get("progress_percent", 0),
                failure if was_draft else row.get("progress_summary", ""),
                "补充原始任务定义后重新进入 Planner 预检。" if was_draft else row.get("progress_next_step"),
                row.get("result_summary"), row.get("result_error"), row.get("result_diagnostic_json"),
                1 if was_draft else row.get("human_required", 0),
                (row.get("human_question") or failure) if was_draft else row.get("human_question"),
                (row.get("human_options_json") if row.get("human_options_json") not in {None, "[]"}
                 else json_dump(["补充任务定义并重新预检", "取消任务"])) if was_draft
                else row.get("human_options_json", "[]"),
                stamp if was_draft else row.get("human_requested_at"),
                row.get("human_responded_at"), row.get("human_response"),
                int(row.get("row_version", 1)) + (1 if was_draft else 0),
            )
            database.execute(
                """INSERT INTO tasks_new(
                  id, title, description, status, priority, estimated_capability_level, capability_level,
                  runtime_environment, provider_id, execution_policy, preflight_status,
                  preflight_execution_id, preflight_started_at, preflight_completed_at, preflight_failure,
                  scope_hint_json, lock_mode, split_suggestions_json, assigned_agent, created_at, started_at,
                  updated_at, heartbeat_at, completed_at, archived_at, attempt, progress_percent,
                  progress_summary, progress_next_step, result_summary, result_error, result_diagnostic_json,
                  human_required, human_question, human_options_json, human_requested_at,
                  human_responded_at, human_response, row_version
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                         ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
            if was_draft:
                old_drafts += 1
                database.execute(
                    "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
                    "VALUES(?, ?, 'DRAFT', 'NEEDS_REVIEW', 'schema-migration', ?)",
                    (row["id"], stamp, failure),
                )
            else:
                if preflight_ready:
                    ready += 1
                    ready_ids.add(row["id"])
        database.execute("DROP TABLE tasks")
        database.execute("ALTER TABLE tasks_new RENAME TO tasks")
        database.execute(
            "CREATE INDEX idx_tasks_queue ON tasks(status, preflight_status, runtime_environment, provider_id, "
            "capability_level, execution_policy, priority, created_at, id)"
        )
        database.execute("CREATE INDEX idx_tasks_preflight ON tasks(status, preflight_status, priority, created_at, id)")
        database.execute("CREATE INDEX idx_tasks_archived ON tasks(archived_at, status, updated_at)")
        _create_preflight_schema_objects(database)
        for row in task_rows:
            if row["id"] in ready_ids:
                database.execute(
                    "INSERT INTO task_technical_acceptance(task_id, ordinal, text) VALUES(?, 0, ?)",
                    (row["id"], "Schema 3.7.0 迁移：沿用既有业务验收与已执行契约。"),
                )
                database.execute(
                    "INSERT INTO task_preflight_evidence(task_id, ordinal, text) VALUES(?, 0, ?)",
                    (row["id"], "Schema 3.7.0 迁移：既有非 DRAFT 任务按 READY 保持队列连续性。"),
                )
        execution_columns = {
            row[1] for row in database.execute("PRAGMA table_info(executions)").fetchall()
        }
        if "execution_kind" not in execution_columns:
            database.execute(
                "ALTER TABLE executions ADD COLUMN execution_kind TEXT NOT NULL DEFAULT 'WORKER' "
                "CHECK (execution_kind = 'WORKER')"
            )
        database.execute(f"PRAGMA user_version = {SCHEMA_USER_VERSION}")
        foreign_key_errors = database.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise LoopError(f"Schema 迁移产生外键错误: {len(foreign_key_errors)}")
        if database.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise LoopError("Schema 迁移后 quick_check 失败")
        commit(database)
    except Exception:
        rollback(database)
        raise
    finally:
        database.execute("PRAGMA foreign_keys = ON")
    return {
        "from": source_version,
        "to": SCHEMA_VERSION,
        "migrated": True,
        "old_drafts_moved_to_review": old_drafts,
        "tasks_marked_ready": ready,
        "active_executions_preserved": active,
    }


def _migrate_recovery_schema(database: sqlite3.Connection) -> dict[str, Any]:
    execution_rows = [
        dict(row) for row in database.execute("SELECT * FROM executions ORDER BY execution_id").fetchall()
    ]
    lock_rows = [
        dict(row) for row in database.execute("SELECT * FROM scope_locks ORDER BY scope_key").fetchall()
    ]
    active = sum(row["status"] == "RUNNING" for row in execution_rows)
    database.execute("PRAGMA foreign_keys = OFF")
    try:
        transaction(database)
        database.execute("ALTER TABLE tasks ADD COLUMN result_diagnostic_json TEXT")
        database.execute("DROP TABLE scope_locks")
        database.execute(EXECUTIONS_TABLE_SQL)
        for row in execution_rows:
            database.execute(
                """INSERT INTO executions_new(
                  execution_id, task_id, status, started_at, heartbeat_at, lease_expires_at, finished_at,
                  outcome, runtime_environment, provider_id, capability_level, execution_policy, model,
                  reasoning, attempt_timeout_seconds, max_retries, termination_reason, recovery_required,
                  recovered_at, recovery_action
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL, NULL)""",
                (
                    row["execution_id"], row["task_id"], row["status"], row["started_at"],
                    row["heartbeat_at"], row["lease_expires_at"], row.get("finished_at"),
                    row.get("outcome"), row["runtime_environment"], row.get("provider_id"),
                    row["capability_level"], row["execution_policy"], row["model"], row["reasoning"],
                    row["attempt_timeout_seconds"], row["max_retries"],
                ),
            )
        database.execute("DROP TABLE executions")
        database.execute("ALTER TABLE executions_new RENAME TO executions")
        database.execute(SCOPE_LOCKS_TABLE_SQL)
        for row in lock_rows:
            database.execute(
                """INSERT INTO scope_locks_new(
                  scope_key, task_id, execution_id, acquired_at, lease_expires_at, status
                ) VALUES(?, ?, ?, ?, ?, 'ACTIVE')""",
                (
                    row["scope_key"], row["task_id"], row["execution_id"],
                    row["acquired_at"], row["lease_expires_at"],
                ),
            )
        database.execute("ALTER TABLE scope_locks_new RENAME TO scope_locks")
        _create_current_execution_indexes(database)
        database.execute(f"PRAGMA user_version = {PREFLIGHT_SCHEMA_USER_VERSION}")
        foreign_key_errors = database.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise LoopError(f"Schema 迁移产生外键错误: {len(foreign_key_errors)}")
        if database.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise LoopError("Schema 迁移后 quick_check 失败")
        commit(database)
    except Exception:
        rollback(database)
        raise
    finally:
        database.execute("PRAGMA foreign_keys = ON")
    return {
        "from": "3.4.0",
        "to": "3.6.0",
        "migrated": True,
        "active_executions_preserved": active,
        "scope_locks_preserved": len(lock_rows),
        "quarantines_created": 0,
    }


def _migrate_diagnostic_schema(database: sqlite3.Connection) -> dict[str, Any]:
    active = int(database.execute("SELECT count(*) FROM executions WHERE status='RUNNING'").fetchone()[0])
    try:
        transaction(database)
        database.execute("ALTER TABLE tasks ADD COLUMN result_diagnostic_json TEXT")
        database.execute(f"PRAGMA user_version = {PREFLIGHT_SCHEMA_USER_VERSION}")
        if database.execute("PRAGMA foreign_key_check").fetchall():
            raise LoopError("Schema 迁移产生外键错误")
        if database.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise LoopError("Schema 迁移后 quick_check 失败")
        commit(database)
    except Exception:
        rollback(database)
        raise
    return {
        "from": "3.5.0",
        "to": "3.6.0",
        "migrated": True,
        "active_executions_preserved": active,
    }


def uses_hybrid_scope_schema(database: sqlite3.Connection) -> bool:
    row = database.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
    ).fetchone()
    if row is None or not row[0]:
        return False
    match = re.search(
        r"lock_mode\s+TEXT\s+CHECK\s*\(lock_mode\s+IS\s+NULL\s+OR\s+lock_mode\s+IN\s*\(([^)]*)\)",
        row[0],
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        return False
    return set(re.findall(r"'([^']+)'", match.group(1))) == set(LOCK_MODES)


def _migrate_hybrid_scope_schema(database: sqlite3.Connection) -> dict[str, Any]:
    active = int(database.execute("SELECT count(*) FROM executions WHERE status='RUNNING'").fetchone()[0])
    old_conflicts = [
        row[0] for row in database.execute(
            "SELECT id FROM tasks WHERE status='WAITING_CONFLICT' ORDER BY id"
        ).fetchall()
    ]
    stamp = now_shanghai()
    database.execute("PRAGMA foreign_keys = OFF")
    try:
        transaction(database)
        database.execute(TASKS_TABLE_SQL)
        database.execute("INSERT INTO tasks_new SELECT * FROM tasks")
        database.execute("DROP TABLE tasks")
        database.execute("ALTER TABLE tasks_new RENAME TO tasks")
        database.execute(
            "CREATE INDEX idx_tasks_queue ON tasks(status, preflight_status, runtime_environment, provider_id, "
            "capability_level, execution_policy, priority, created_at, id)"
        )
        database.execute(
            "CREATE INDEX idx_tasks_preflight ON tasks(status, preflight_status, priority, created_at, id)"
        )
        database.execute("CREATE INDEX idx_tasks_archived ON tasks(archived_at, status, updated_at)")
        for task_id in old_conflicts:
            database.execute(
                "UPDATE tasks SET status='PENDING', updated_at=?, "
                "progress_summary='旧 WAITING_CONFLICT 已转换为动态 scope 排队。', "
                "progress_next_step='等待 scope 可用后由下一次 claim 自动领取。', "
                "row_version=row_version+1 WHERE id=?",
                (stamp, task_id),
            )
            database.execute(
                "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
                "VALUES(?, ?, 'WAITING_CONFLICT', 'PENDING', 'schema-migration', "
                "'混合 scope 锁迁移：保留原 blocker 记录，改用 PENDING 动态排队。')",
                (task_id, stamp),
            )
        if database.execute("PRAGMA foreign_key_check").fetchall():
            raise LoopError("混合 scope 锁迁移产生外键错误")
        if database.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise LoopError("混合 scope 锁迁移后 quick_check 失败")
        commit(database)
    except Exception:
        rollback(database)
        raise
    finally:
        database.execute("PRAGMA foreign_keys = ON")
    return {
        "from": SCHEMA_VERSION,
        "to": SCHEMA_VERSION,
        "migrated": True,
        "hybrid_scope_lock_migrated": True,
        "waiting_conflicts_requeued": old_conflicts,
        "active_executions_preserved": active,
    }


def migrate_schema(database: sqlite3.Connection) -> dict[str, Any]:
    current = int(database.execute("PRAGMA user_version").fetchone()[0])
    if current == SCHEMA_USER_VERSION:
        if not uses_hybrid_scope_schema(database):
            return _migrate_hybrid_scope_schema(database)
        return {
            "from": SCHEMA_VERSION, "to": SCHEMA_VERSION, "migrated": False,
            "archived": 0, "tasks_mapped": 0, "executions_snapshotted": 0,
        }
    if current == PREFLIGHT_SCHEMA_USER_VERSION:
        return _migrate_preflight_schema(database)
    if current == DIAGNOSTIC_SCHEMA_USER_VERSION:
        first = _migrate_diagnostic_schema(database)
        return {**_migrate_preflight_schema(database, first["from"]), **{
            key: value for key, value in first.items() if key not in {"from", "to", "migrated"}
        }}
    if current == RECOVERY_SCHEMA_USER_VERSION:
        first = _migrate_recovery_schema(database)
        return {**_migrate_preflight_schema(database, first["from"]), **{
            key: value for key, value in first.items() if key not in {"from", "to", "migrated"}
        }}
    supported_versions = {
        LEGACY_SCHEMA_USER_VERSION,
        ARCHIVE_SCHEMA_USER_VERSION,
        ROUTING_SCHEMA_USER_VERSION,
        PROFILE_ROUTING_SCHEMA_USER_VERSION,
    }
    if current not in supported_versions:
        raise LoopError(
            "不支持的 Schema 迁移: "
            f"user_version={current}, expected one of {sorted(supported_versions)}"
        )
    active = int(database.execute("SELECT count(*) FROM executions WHERE status='RUNNING'").fetchone()[0])
    if active:
        raise LoopError(f"Schema 迁移要求没有活动 execution，当前为 {active}")

    source_version = {
        LEGACY_SCHEMA_USER_VERSION: "3.0.0",
        ARCHIVE_SCHEMA_USER_VERSION: "3.1.0",
        ROUTING_SCHEMA_USER_VERSION: "3.2.0",
        PROFILE_ROUTING_SCHEMA_USER_VERSION: "3.3.0",
    }[current]
    config = load_initialization_config()
    database.execute("PRAGMA foreign_keys = OFF")
    try:
        transaction(database)
        columns = {row[1] for row in database.execute("PRAGMA table_info(tasks)").fetchall()}
        if "archived_at" not in columns:
            database.execute("ALTER TABLE tasks ADD COLUMN archived_at TEXT")
        confirmed = database.execute(
            "SELECT id, status, COALESCE(completed_at, updated_at, created_at) AS archived_at "
            "FROM tasks WHERE status='CONFIRMED' AND archived_at IS NULL"
        ).fetchall() if current == LEGACY_SCHEMA_USER_VERSION else []
        reason = "Schema 3.1.0 迁移：按旧版 CONFIRMED 等同已归档的语义设置 archived_at。"
        for task in confirmed:
            database.execute(
                "UPDATE tasks SET archived_at=?, row_version=row_version+1 WHERE id=?",
                (task["archived_at"], task["id"]),
            )
            database.execute(
                "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
                "VALUES(?, ?, ?, ?, 'schema-migration', ?)",
                (task["id"], now_shanghai(), task["status"], task["status"], reason),
            )

        task_rows = [dict(row) for row in database.execute("SELECT * FROM tasks ORDER BY id").fetchall()]
        execution_rows = [
            dict(row) for row in database.execute("SELECT * FROM executions ORDER BY execution_id").fetchall()
        ]
        normalized_tasks: dict[str, dict[str, Any]] = {}
        for row in task_rows:
            legacy_profile = row.get("execution_profile", "standard")
            legacy_environment = row.get("runtime_environment", "codex_automation")
            runtime_environment, provider_id = normalize_execution_target(legacy_environment)
            normalized_tasks[row["id"]] = {
                "capability_level": LEGACY_PROFILE_TO_CAPABILITY[legacy_profile],
                "runtime_environment": runtime_environment,
                "provider_id": provider_id,
                "execution_policy": "manual" if legacy_profile == "exceptional" else "automatic",
            }

        database.execute(TASKS_TABLE_SQL)
        draft_migrations: list[tuple[str, str]] = []
        legacy_ready_ids: set[str] = set()
        for row in task_rows:
            route = normalized_tasks[row["id"]]
            was_draft = row["status"] == "DRAFT"
            status = "NEEDS_REVIEW" if was_draft else row["status"]
            scope_hint = [
                item[0] for item in database.execute(
                    "SELECT scope FROM task_scopes WHERE task_id=? ORDER BY ordinal", (row["id"],)
                ).fetchall()
            ]
            preflight_ready = not was_draft and bool(scope_hint)
            failure = (
                "Schema 3.7.0 迁移：旧 DRAFT 的边界未经过 Planner 预检，需要人工补充后重新预检。"
                if was_draft else (
                    "Schema 3.7.0 迁移：历史终态缺少精确 scope，未声明为 READY。"
                    if not preflight_ready else None
                )
            )
            database.execute(
                """INSERT INTO tasks_new(
                  id, title, description, status, priority, estimated_capability_level, capability_level,
                  runtime_environment, provider_id, execution_policy, preflight_status,
                  preflight_execution_id, preflight_started_at, preflight_completed_at, preflight_failure,
                  scope_hint_json, lock_mode, split_suggestions_json, assigned_agent, created_at, started_at,
                  updated_at, heartbeat_at, completed_at, archived_at, attempt, progress_percent,
                  progress_summary, progress_next_step, result_summary, result_error, result_diagnostic_json,
                  human_required, human_question, human_options_json, human_requested_at,
                  human_responded_at, human_response, row_version
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                         ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["id"], row["title"], row.get("description", ""), status, row["priority"],
                    route["capability_level"], None if was_draft else route["capability_level"],
                    route["runtime_environment"], route["provider_id"], route["execution_policy"],
                    "READY" if preflight_ready else "FAILED", None, None,
                    now_shanghai() if preflight_ready else None, failure, json_dump(scope_hint),
                    "project" if preflight_ready else None, "[]", row.get("assigned_agent"), row["created_at"],
                    row.get("started_at"), row["updated_at"], row.get("heartbeat_at"),
                    row.get("completed_at"), row.get("archived_at"), row.get("attempt", 0),
                    row.get("progress_percent", 0), failure if was_draft else row.get("progress_summary", ""),
                    "补充原始任务定义后重新进入 Planner 预检。" if was_draft else row.get("progress_next_step"),
                    row.get("result_summary"), row.get("result_error"), None,
                    1 if was_draft else row.get("human_required", 0),
                    (row.get("human_question") or failure) if was_draft else row.get("human_question"),
                    json_dump(["补充任务定义并重新预检", "取消任务"]) if was_draft
                    else row.get("human_options_json", "[]"),
                    now_shanghai() if was_draft else row.get("human_requested_at"),
                    row.get("human_responded_at"), row.get("human_response"),
                    int(row.get("row_version", 1)) + (1 if was_draft else 0),
                ),
            )
            if was_draft:
                draft_migrations.append((row["id"], failure))
            elif preflight_ready:
                legacy_ready_ids.add(row["id"])
        database.execute(EXECUTIONS_TABLE_SQL)
        for row in execution_rows:
            route = normalized_tasks[row["task_id"]]
            snapshot = resolve_execution_profile(
                route["runtime_environment"], route["provider_id"], route["capability_level"], config
            )
            database.execute(
                """INSERT INTO executions_new(
                  execution_id, task_id, status, started_at, heartbeat_at, lease_expires_at, finished_at,
                  outcome, runtime_environment, provider_id, capability_level, execution_policy, model,
                  reasoning, attempt_timeout_seconds, max_retries
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["execution_id"], row["task_id"], row["status"], row["started_at"],
                    row["heartbeat_at"], row["lease_expires_at"], row.get("finished_at"), row.get("outcome"),
                    snapshot["runtime_environment"], snapshot["provider_id"], snapshot["capability_level"],
                    route["execution_policy"], snapshot["model"], snapshot["reasoning"],
                    snapshot["attempt_timeout_seconds"], snapshot["max_retries"],
                ),
            )
        database.execute("DROP TABLE executions")
        database.execute("DROP TABLE tasks")
        database.execute("ALTER TABLE tasks_new RENAME TO tasks")
        database.execute("ALTER TABLE executions_new RENAME TO executions")
        database.execute(
            "CREATE INDEX idx_tasks_queue ON tasks(status, preflight_status, runtime_environment, provider_id, "
            "capability_level, execution_policy, priority, created_at, id)"
        )
        database.execute("CREATE INDEX idx_tasks_preflight ON tasks(status, preflight_status, priority, created_at, id)")
        database.execute(
            "CREATE INDEX idx_tasks_archived ON tasks(archived_at, status, updated_at)"
        )
        database.execute(
            "ALTER TABLE scope_locks ADD COLUMN status TEXT NOT NULL DEFAULT 'ACTIVE' "
            "CHECK (status IN ('ACTIVE', 'QUARANTINED'))"
        )
        database.execute("ALTER TABLE scope_locks ADD COLUMN quarantined_at TEXT")
        database.execute("ALTER TABLE scope_locks ADD COLUMN quarantine_reason TEXT")
        _create_current_execution_indexes(database)
        _create_preflight_schema_objects(database)
        for row in task_rows:
            if row["id"] in legacy_ready_ids:
                database.execute(
                    "INSERT INTO task_technical_acceptance(task_id, ordinal, text) VALUES(?, 0, ?)",
                    (row["id"], "Schema 3.7.0 迁移：沿用既有业务验收与已执行契约。"),
                )
                database.execute(
                    "INSERT INTO task_preflight_evidence(task_id, ordinal, text) VALUES(?, 0, ?)",
                    (row["id"], "Schema 3.7.0 迁移：既有非 DRAFT 任务按 READY 保持队列连续性。"),
                )
        for task_id, failure in draft_migrations:
            database.execute(
                "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) "
                "VALUES(?, ?, 'DRAFT', 'NEEDS_REVIEW', 'schema-migration', ?)",
                (task_id, now_shanghai(), failure),
            )
        database.execute(f"PRAGMA user_version = {SCHEMA_USER_VERSION}")
        foreign_key_errors = database.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise LoopError(f"Schema 迁移产生外键错误: {len(foreign_key_errors)}")
        if database.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise LoopError("Schema 迁移后 quick_check 失败")
        commit(database)
    except Exception:
        rollback(database)
        raise
    finally:
        database.execute("PRAGMA foreign_keys = ON")
    return {
        "from": source_version,
        "to": SCHEMA_VERSION,
        "migrated": True,
        "archived": len(confirmed),
        "tasks_mapped": len(task_rows),
        "executions_snapshotted": len(execution_rows),
        "old_drafts_moved_to_review": len(draft_migrations),
        "tasks_marked_ready": len(legacy_ready_ids),
    }


def schema_version(database: sqlite3.Connection) -> str:
    value = int(database.execute("PRAGMA user_version").fetchone()[0])
    return SCHEMA_VERSION if value == SCHEMA_USER_VERSION else str(value)


def transaction(database: sqlite3.Connection) -> None:
    database.execute("BEGIN IMMEDIATE")


def commit(database: sqlite3.Connection) -> None:
    database.execute("COMMIT")


def rollback(database: sqlite3.Connection) -> None:
    if database.in_transaction:
        database.execute("ROLLBACK")


def execution_setting(key: str, default: Any = None, config: dict[str, Any] | None = None) -> Any:
    value = config or load_initialization_config()
    return value.get("task_execution", {}).get(key, default)


def global_parallel_limit(config: dict[str, Any] | None = None) -> int:
    return int(execution_setting("global_max_active_executions", 8, config))


def platform_parallel_limit(platform: str, config: dict[str, Any] | None = None) -> int:
    if platform not in CANONICAL_RUNTIME_ENVIRONMENTS:
        raise LoopError(f"执行平台无效: {platform}")
    limits = execution_setting("platform_max_active_executions", {}, config)
    return int(limits[platform])


def uses_capability_schema(database: sqlite3.Connection) -> bool:
    columns = {row[1] for row in database.execute("PRAGMA table_info(tasks)").fetchall()}
    return "capability_level" in columns


def uses_recovery_schema(database: sqlite3.Connection) -> bool:
    execution_columns = {row[1] for row in database.execute("PRAGMA table_info(executions)").fetchall()}
    lock_columns = {row[1] for row in database.execute("PRAGMA table_info(scope_locks)").fetchall()}
    return "recovery_required" in execution_columns and "status" in lock_columns


def uses_result_diagnostic_schema(database: sqlite3.Connection) -> bool:
    columns = {row[1] for row in database.execute("PRAGMA table_info(tasks)").fetchall()}
    return "result_diagnostic_json" in columns


def uses_preflight_schema(database: sqlite3.Connection) -> bool:
    columns = {row[1] for row in database.execute("PRAGMA table_info(tasks)").fetchall()}
    return "preflight_status" in columns


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


def _dependencies_ready_for_projection(database: sqlite3.Connection, task_id: str) -> bool:
    return database.execute(
        "SELECT 1 FROM task_dependencies d JOIN tasks t ON t.id=d.dependency_id "
        "WHERE d.task_id=? AND t.status NOT IN ('SUCCEEDED','CONFIRMED') LIMIT 1",
        (task_id,),
    ).fetchone() is None


def scope_queue_position(
    database: sqlite3.Connection,
    task_id: str,
    scope_keys: Iterable[str],
) -> int | None:
    target_keys = list(scope_keys)
    if not target_keys or not _dependencies_ready_for_projection(database, task_id):
        return None
    candidates = database.execute(
        "SELECT id FROM tasks WHERE status='PENDING' AND preflight_status='READY' ORDER BY "
        "CASE priority WHEN 'blocker' THEN 0 WHEN 'critical' THEN 1 WHEN 'high' THEN 2 "
        "WHEN 'medium' THEN 3 ELSE 4 END, created_at, id"
    ).fetchall()
    position = 0
    for candidate in candidates:
        candidate_id = candidate["id"]
        if not _dependencies_ready_for_projection(database, candidate_id):
            continue
        candidate_keys = task_children(database, candidate_id, "task_scopes", "scope_key")
        if not any(scope_keys_conflict(left, right) for left in target_keys for right in candidate_keys):
            continue
        position += 1
        if candidate_id == task_id:
            return position
    return None


def replace_ordered_text(
    database: sqlite3.Connection, table: str, task_id: str, values: Iterable[str]
) -> None:
    database.execute(f"DELETE FROM {table} WHERE task_id=?", (task_id,))
    database.executemany(
        f"INSERT INTO {table}(task_id, ordinal, text) VALUES(?, ?, ?)",
        [(task_id, index, str(value)) for index, value in enumerate(values)],
    )


def task_exists(database: sqlite3.Connection, task_id: str) -> bool:
    return database.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is not None


def dependency_cycle_path(
    database: sqlite3.Connection,
    replacement_task_id: str | None = None,
    replacement_dependencies: Iterable[str] | None = None,
) -> list[str] | None:
    graph: dict[str, list[str]] = {
        row[0]: [] for row in database.execute("SELECT id FROM tasks ORDER BY id").fetchall()
    }
    for row in database.execute(
        "SELECT task_id, dependency_id FROM task_dependencies ORDER BY task_id, dependency_id"
    ).fetchall():
        graph.setdefault(row["task_id"], []).append(row["dependency_id"])
    if replacement_task_id is not None:
        graph[replacement_task_id] = list(replacement_dependencies or [])

    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def visit(task_id: str) -> list[str] | None:
        visiting.add(task_id)
        stack.append(task_id)
        for dependency in graph.get(task_id, []):
            if dependency in visiting:
                start = stack.index(dependency)
                return stack[start:] + [dependency]
            if dependency not in visited:
                cycle = visit(dependency)
                if cycle:
                    return cycle
        stack.pop()
        visiting.remove(task_id)
        visited.add(task_id)
        return None

    for task_id in sorted(graph):
        if task_id not in visited:
            cycle = visit(task_id)
            if cycle:
                return cycle
    return None


def set_task_dependencies(
    database: sqlite3.Connection, task_id: str, dependencies: Iterable[str]
) -> None:
    values = [str(dependency) for dependency in dependencies]
    if len(values) != len(set(values)):
        raise LoopError("任务依赖不能重复")
    if task_id in values:
        raise LoopError(f"任务不能依赖自身: {task_id} -> {task_id}")
    missing = [dependency for dependency in values if not task_exists(database, dependency)]
    if missing:
        raise LoopError("依赖任务不存在: " + ", ".join(missing))
    cycle = dependency_cycle_path(database, task_id, values)
    if cycle:
        raise LoopError("循环依赖: " + " -> ".join(cycle))
    database.execute("DELETE FROM task_dependencies WHERE task_id=?", (task_id,))
    database.executemany(
        "INSERT INTO task_dependencies(task_id, dependency_id) VALUES(?, ?)",
        [(task_id, dependency) for dependency in values],
    )


def insert_task(
    database: sqlite3.Connection,
    task: dict[str, Any],
    actor: str = "migration",
    project_paths: Iterable[str] | None = None,
) -> None:
    task_id = task.get("id")
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Z][A-Z0-9_-]*", task_id):
        raise LoopError(f"任务 id 无效: {task_id}")
    if task_exists(database, task_id):
        raise LoopError(f"任务 id 已存在: {task_id}")
    status = task.get("status", "PENDING")
    priority = task.get("priority", "medium")
    execution_profile = task.get("execution_profile")
    capability_level = task.get("capability_level")
    estimated_capability_level = task.get("estimated_capability_level")
    execution_policy = task.get("execution_policy")
    preflight_schema = uses_preflight_schema(database)
    config = load_initialization_config()
    runtime_environment = task.get("runtime_environment")
    if runtime_environment is None and preflight_schema:
        runtime_environment = config["planner"]["default_runtime_environment"]
    provider_id = task.get("provider_id")
    valid_statuses = {
        "DRAFT", "NEEDS_REVIEW", "PENDING", "RUNNING", "WAITING_CONFLICT", "WAITING_HUMAN",
        "SUCCEEDED", "CONFIRMED", "FAILED", "CANCELLED",
    }
    if status not in valid_statuses or (not preflight_schema and status == "NEEDS_REVIEW"):
        raise LoopError(f"任务状态无效: {status}")
    if priority not in PRIORITIES:
        raise LoopError(f"任务优先级无效: {priority}")
    if execution_profile is not None and execution_profile not in EXECUTION_PROFILES:
        raise LoopError(f"执行档位无效: {execution_profile}")
    if estimated_capability_level is None:
        estimated_capability_level = capability_level
    if capability_level is None and execution_profile is not None:
        capability_level = LEGACY_PROFILE_TO_CAPABILITY[execution_profile]
    if preflight_schema and status not in {"DRAFT", "NEEDS_REVIEW"} and capability_level is None:
        capability_level = "L2"
    if not preflight_schema and capability_level is None:
        capability_level = "L2"
    if estimated_capability_level is None:
        estimated_capability_level = capability_level
    if estimated_capability_level is not None and estimated_capability_level not in CAPABILITY_LEVELS:
        raise LoopError(f"任务预估能力等级无效: {estimated_capability_level}")
    if capability_level is not None and capability_level not in CAPABILITY_LEVELS:
        raise LoopError(f"任务能力等级无效: {capability_level}")
    if execution_policy is None:
        execution_policy = "manual" if execution_profile == "exceptional" else "automatic"
    if execution_policy not in EXECUTION_POLICIES:
        raise LoopError(f"执行策略无效: {execution_policy}")
    comparison_level = capability_level or estimated_capability_level
    if execution_profile is not None and (
        comparison_level is None or legacy_profile_for(comparison_level, execution_policy) != execution_profile
    ):
        raise LoopError("旧 execution_profile 与 capability_level/execution_policy 不一致")
    runtime_environment, provider_id = normalize_execution_target(runtime_environment, provider_id)
    if capability_level is not None:
        resolve_execution_profile(runtime_environment, provider_id, capability_level)
    stamp = task.get("created_at") or now_shanghai()
    progress = task.get("progress") or {}
    result = task.get("result") or {}
    diagnostic = normalize_result_diagnostic(result.get("diagnostic"))
    diagnostic_json = json_dump(diagnostic) if diagnostic is not None else None
    human = task.get("human_intervention") or {}
    common_tail = (
        task.get("assigned_agent"), stamp, task.get("started_at"), task.get("updated_at") or stamp,
        task.get("heartbeat_at"), task.get("completed_at"), task.get("archived_at"),
        int(task.get("attempt", 0)), int(progress.get("percent", 0)),
        str(progress.get("summary") or ""), progress.get("next_step"), result.get("summary"),
        result.get("error"), int(bool(human.get("required", False))), human.get("question"),
        json_dump(human.get("options") or []), human.get("requested_at"), human.get("responded_at"),
        human.get("response"), int(task.get("row_version", 1)),
    )
    if preflight_schema:
        if status == "DRAFT":
            preflight_status = task.get("preflight_status", "UNINSPECTED")
        elif status == "NEEDS_REVIEW":
            preflight_status = task.get("preflight_status", "FAILED")
        else:
            preflight_status = task.get("preflight_status", "READY")
        if preflight_status not in PREFLIGHT_STATUSES:
            raise LoopError(f"preflight_status 无效: {preflight_status}")
        if status == "DRAFT" and preflight_status not in {"UNINSPECTED", "INSPECTING"}:
            raise LoopError("DRAFT 只能处于 UNINSPECTED 或 INSPECTING")
        if status == "NEEDS_REVIEW" and preflight_status != "FAILED":
            raise LoopError("NEEDS_REVIEW 必须处于 FAILED preflight")
        if status not in {"DRAFT", "NEEDS_REVIEW"} and preflight_status != "READY":
            raise LoopError(f"{status} 任务必须处于 READY preflight")
        scope_input = normalize_string_list(task.get("scope") or [], "scope")
        scope_hint = normalize_string_list(task.get("scope_hint", scope_input), "scope_hint")
        exact_scopes = scope_input if preflight_status == "READY" else []
        technical_acceptance = normalize_string_list(
            task.get("technical_acceptance")
            or ((task.get("acceptance") or ["既有任务按兼容契约进入 READY。"])
                if preflight_status == "READY" else []),
            "technical_acceptance",
        )
        evidence = normalize_string_list(
            task.get("preflight_evidence")
            or (["任务由受控导入路径按 READY 建立。"] if preflight_status == "READY" else []),
            "preflight_evidence",
        )
        lock_mode = task.get("lock_mode", "project" if preflight_status == "READY" else None)
        if lock_mode is not None and lock_mode not in LOCK_MODES:
            raise LoopError(f"lock_mode 无效: {lock_mode}")
        split_suggestions = normalize_split_suggestions(task.get("split_suggestions"))
        if preflight_status == "READY":
            if capability_level is None or not exact_scopes or lock_mode is None:
                raise LoopError("READY 任务必须具备最终 capability_level、scope 和 lock_mode")
            if not technical_acceptance or not evidence:
                raise LoopError("READY 任务必须具备技术验收补充和检查证据")
            if split_suggestions:
                raise LoopError("READY 任务不得保留拆分建议")
        elif status in {"DRAFT", "NEEDS_REVIEW"}:
            capability_level = None
            lock_mode = None
        columns = (
            "id, title, description, status, priority, estimated_capability_level, capability_level, "
            "runtime_environment, provider_id, execution_policy, preflight_status, preflight_execution_id, "
            "preflight_started_at, preflight_completed_at, preflight_failure, scope_hint_json, lock_mode, "
            "split_suggestions_json, assigned_agent, created_at, started_at, updated_at, heartbeat_at, "
            "completed_at, archived_at, attempt, progress_percent, progress_summary, progress_next_step, "
            "result_summary, result_error, result_diagnostic_json, human_required, human_question, "
            "human_options_json, human_requested_at, human_responded_at, human_response, row_version"
        )
        values = (
            task_id, str(task.get("title") or task_id), str(task.get("description") or ""), status,
            priority, estimated_capability_level, capability_level, runtime_environment, provider_id,
            execution_policy, preflight_status, task.get("preflight_execution_id"),
            task.get("preflight_started_at"), task.get("preflight_completed_at"),
            task.get("preflight_failure"), json_dump(scope_hint), lock_mode,
            json_dump(split_suggestions), *common_tail[:13], diagnostic_json, *common_tail[13:],
        )
        placeholders = ", ".join("?" for _ in values)
        database.execute(f"INSERT INTO tasks({columns}) VALUES({placeholders})", values)
    elif uses_capability_schema(database):
        if capability_level is None:
            raise LoopError("当前 Schema 的任务能力等级不能为空")
        columns = (
            "id, title, description, status, priority, capability_level, runtime_environment, provider_id, "
            "execution_policy, assigned_agent, created_at, started_at, updated_at, heartbeat_at, "
            "completed_at, archived_at, attempt, progress_percent, progress_summary, progress_next_step, "
            "result_summary, result_error"
        )
        values = (
            task_id, str(task.get("title") or task_id), str(task.get("description") or ""), status,
            priority, capability_level, runtime_environment, provider_id, execution_policy,
            *common_tail[:13],
        )
        if uses_result_diagnostic_schema(database):
            columns += ", result_diagnostic_json"
            values += (diagnostic_json,)
        columns += (
            ", human_required, human_question, human_options_json, human_requested_at, "
            "human_responded_at, human_response, row_version"
        )
        values += common_tail[13:]
        placeholders = ", ".join("?" for _ in values)
        database.execute(f"INSERT INTO tasks({columns}) VALUES({placeholders})", values)
    else:
        if execution_policy == "manual" and capability_level != "L5":
            raise LoopError("Schema 3.3.0 兼容层无法表示 L1-L4 manual execution_policy")
        if runtime_environment == "self_hosted_agent":
            if provider_id != "deepseek":
                raise LoopError("Schema 3.3.0 兼容层只支持 self_hosted_agent/deepseek")
            legacy_environment = "deepseek"
        else:
            legacy_environment = runtime_environment
        legacy_profile = legacy_profile_for(capability_level, execution_policy)
        database.execute(
            """INSERT INTO tasks(
              id, title, description, status, priority, execution_profile, runtime_environment, assigned_agent,
              created_at, started_at, updated_at, heartbeat_at, completed_at, archived_at, attempt,
              progress_percent, progress_summary, progress_next_step, result_summary, result_error,
              human_required, human_question, human_options_json, human_requested_at,
              human_responded_at, human_response, row_version
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id, str(task.get("title") or task_id), str(task.get("description") or ""), status,
                priority, legacy_profile, legacy_environment, *common_tail,
            ),
        )
    set_task_dependencies(database, task_id, task.get("depends_on") or [])
    scopes_to_store = exact_scopes if preflight_schema else normalize_string_list(task.get("scope") or [], "scope")
    stored_lock_mode = lock_mode if preflight_schema else "project"
    normalized_scopes = [
        normalize_scope(scope, stored_lock_mode, project_paths) for scope in scopes_to_store
    ]
    normalized_keys = [item["scope_key"] for item in normalized_scopes]
    if len(normalized_keys) != len(set(normalized_keys)) and stored_lock_mode != "project":
        raise LoopError("scope 规范化后不能重复")
    for index, item in enumerate(normalized_scopes):
        database.execute(
            "INSERT INTO task_scopes(task_id, ordinal, scope, scope_key) VALUES(?, ?, ?, ?)",
            (task_id, index, item["scope"], item["scope_key"]),
        )
    replace_ordered_text(database, "task_acceptance", task_id, task.get("acceptance") or [])
    if preflight_schema:
        replace_ordered_text(database, "task_technical_acceptance", task_id, technical_acceptance)
        replace_ordered_text(database, "task_preflight_evidence", task_id, evidence)
    replace_ordered_text(database, "task_completed_items", task_id, progress.get("completed") or [])
    replace_ordered_text(database, "task_verifications", task_id, result.get("verification") or [])
    for index, attachment in enumerate(task.get("attachments") or []):
        database.execute(
            "INSERT INTO task_attachments(task_id, ordinal, path, sha256, role, saved_at) VALUES(?, ?, ?, ?, ?, ?)",
            (task_id, index, attachment.get("path"), attachment.get("sha256"),
             attachment.get("role", "source"), attachment.get("saved_at") or stamp),
        )
    history = task.get("history") or [
        {"at": stamp, "from": None, "to": status, "actor": actor, "reason": "任务已创建。"}
    ]
    for entry in history:
        database.execute(
            "INSERT INTO task_history(task_id, at, from_status, to_status, actor, reason) VALUES(?, ?, ?, ?, ?, ?)",
            (task_id, entry.get("at") or stamp, entry.get("from"), entry.get("to") or status,
             entry.get("actor") or actor, entry.get("reason") or "状态迁移。"),
        )


def task_children(
    database: sqlite3.Connection,
    task_id: str,
    table: str,
    column: str = "text",
    order_column: str = "ordinal",
) -> list[Any]:
    rows = database.execute(
        f"SELECT {column} FROM {table} WHERE task_id=? ORDER BY {order_column}", (task_id,)
    ).fetchall()
    return [row[0] for row in rows]


def task_dict(database: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    task_id = row["id"]
    attachments = [dict(item) for item in database.execute(
        "SELECT path, sha256, role, saved_at FROM task_attachments WHERE task_id=? ORDER BY ordinal",
        (task_id,),
    ).fetchall()]
    history = [{
        "at": item["at"], "from": item["from_status"], "to": item["to_status"],
        "actor": item["actor"], "reason": item["reason"],
    } for item in database.execute(
        "SELECT at, from_status, to_status, actor, reason FROM task_history WHERE task_id=? ORDER BY id",
        (task_id,),
    ).fetchall()]
    conflicts = [dict(item) for item in database.execute(
        "SELECT scope_key, blocker_task_id, blocker_execution_id, detected_at FROM task_conflicts WHERE task_id=? ORDER BY scope_key",
        (task_id,),
    ).fetchall()]
    columns = set(row.keys())
    if "capability_level" in columns:
        capability_level = row["capability_level"]
        execution_policy = row["execution_policy"]
        provider_id = row["provider_id"]
        runtime_environment = row["runtime_environment"]
        execution_profile = (
            legacy_profile_for(capability_level, execution_policy) if capability_level is not None else None
        )
    else:
        execution_profile = row["execution_profile"]
        capability_level = LEGACY_PROFILE_TO_CAPABILITY[execution_profile]
        execution_policy = "manual" if execution_profile == "exceptional" else "automatic"
        legacy_environment = row["runtime_environment"]
        runtime_environment = "self_hosted_agent" if legacy_environment == "deepseek" else legacy_environment
        provider_id = "deepseek" if legacy_environment == "deepseek" else None
    scopes = task_children(database, task_id, "task_scopes", "scope")
    scope_keys = task_children(database, task_id, "task_scopes", "scope_key")
    acceptance = task_children(database, task_id, "task_acceptance")
    dependencies = task_children(database, task_id, "task_dependencies", "dependency_id", "dependency_id")
    if "preflight_status" in columns:
        scope_hint = normalize_string_list(json_load(row["scope_hint_json"], []), "scope_hint")
        split_suggestions = normalize_split_suggestions(json_load(row["split_suggestions_json"], []))
        technical_acceptance = task_children(database, task_id, "task_technical_acceptance")
        evidence = task_children(database, task_id, "task_preflight_evidence")
        preflight_status = row["preflight_status"]
        estimated_capability_level = row["estimated_capability_level"]
        lock_mode = row["lock_mode"]
        preflight_execution_id = row["preflight_execution_id"]
        preflight_started_at = row["preflight_started_at"]
        preflight_completed_at = row["preflight_completed_at"]
        preflight_failure = row["preflight_failure"]
    else:
        scope_hint = scopes
        split_suggestions = []
        technical_acceptance = acceptance
        evidence = []
        preflight_status = "READY"
        estimated_capability_level = capability_level
        lock_mode = "project"
        preflight_execution_id = None
        preflight_started_at = None
        preflight_completed_at = row["updated_at"]
        preflight_failure = None
    operator_definition = {
        "description": row["description"],
        "acceptance": acceptance,
        "priority": row["priority"],
        "runtime_environment": runtime_environment,
        "provider_id": provider_id,
        "execution_policy": execution_policy,
        "depends_on": dependencies,
        "attachments": attachments,
        "scope_hint": scope_hint,
        "estimated_capability_level": estimated_capability_level,
    }
    planner_supplement = {
        "preflight_status": preflight_status,
        "execution_id": preflight_execution_id,
        "started_at": preflight_started_at,
        "completed_at": preflight_completed_at,
        "failure": preflight_failure,
        "capability_level": capability_level,
        "scope": scopes,
        "lock_mode": lock_mode,
        "technical_acceptance": technical_acceptance,
        "evidence": evidence,
        "split_suggestions": split_suggestions,
    }
    blocking_scopes = (
        scope_conflicts_for_keys(database, scope_keys, exclude_task_id=task_id)
        if row["status"] == "PENDING" else []
    )
    blocked_by_task_ids = sorted({item["blocker_task_id"] for item in blocking_scopes})
    blocked_scope_keys = sorted({item["requested_scope_key"] for item in blocking_scopes})
    blocked_key_set = set(blocked_scope_keys)
    blocked_scopes = sorted({
        scope for scope, scope_key in zip(scopes, scope_keys) if scope_key in blocked_key_set
    })
    queue_position = (
        scope_queue_position(database, task_id, scope_keys) if row["status"] == "PENDING" else None
    )
    return {
        "id": task_id, "title": row["title"], "description": row["description"],
        "status": row["status"], "priority": row["priority"],
        "preflight_status": preflight_status,
        "estimated_capability_level": estimated_capability_level,
        "capability_level": capability_level, "execution_policy": execution_policy,
        "provider_id": provider_id, "runtime_environment": runtime_environment,
        "execution_profile": execution_profile,
        "assigned_agent": row["assigned_agent"],
        "created_at": row["created_at"], "started_at": row["started_at"], "updated_at": row["updated_at"],
        "heartbeat_at": row["heartbeat_at"], "completed_at": row["completed_at"],
        "archived_at": row["archived_at"], "attempt": row["attempt"],
        "depends_on": dependencies,
        "scope_hint": scope_hint,
        "scope": scopes,
        "scope_keys": scope_keys,
        "lock_mode": lock_mode,
        "blocked_by_task_ids": blocked_by_task_ids,
        "blocked_scopes": blocked_scopes,
        "blocked_scope_keys": blocked_scope_keys,
        "blocking_scopes": blocking_scopes,
        "scope_queue_position": queue_position,
        "acceptance": acceptance,
        "technical_acceptance": technical_acceptance,
        "preflight_evidence": evidence,
        "split_suggestions": split_suggestions,
        "operator_definition": operator_definition,
        "planner_supplement": planner_supplement,
        "progress": {
            "percent": row["progress_percent"], "summary": row["progress_summary"],
            "completed": task_children(database, task_id, "task_completed_items"),
            "next_step": row["progress_next_step"],
        },
        "human_intervention": {
            "required": bool(row["human_required"]), "question": row["human_question"],
            "options": json_load(row["human_options_json"], []), "requested_at": row["human_requested_at"],
            "responded_at": row["human_responded_at"], "response": row["human_response"],
        },
        "attachments": attachments,
        "result": {
            "summary": row["result_summary"],
            "verification": task_children(database, task_id, "task_verifications"),
            "error": row["result_error"],
            "diagnostic": (
                load_result_diagnostic(row["result_diagnostic_json"])
                if "result_diagnostic_json" in columns and row["result_diagnostic_json"] is not None
                else None
            ),
        },
        "history": history, "conflicts": conflicts, "row_version": row["row_version"],
    }


def all_tasks(database: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = database.execute(
        "SELECT * FROM tasks ORDER BY CASE priority WHEN 'blocker' THEN 0 WHEN 'critical' THEN 1 "
        "WHEN 'high' THEN 2 WHEN 'medium' THEN 3 ELSE 4 END, created_at, id"
    ).fetchall()
    return [task_dict(database, row) for row in rows]


def current_revision(database: sqlite3.Connection) -> int:
    task_versions = int(database.execute("SELECT COALESCE(sum(row_version), 0) FROM tasks").fetchone()[0])
    histories = int(database.execute("SELECT count(*) FROM task_history").fetchone()[0])
    executions = int(database.execute("SELECT count(*) FROM executions").fetchone()[0])
    preflight_executions = (
        int(database.execute("SELECT count(*) FROM preflight_executions").fetchone()[0])
        if uses_preflight_schema(database) else 0
    )
    return task_versions + histories + executions + preflight_executions


def state_payload(database: sqlite3.Connection, config: dict[str, Any] | None = None) -> dict[str, Any]:
    value = config or load_initialization_config()
    if uses_capability_schema(database):
        executions = database.execute(
            "SELECT execution_id, task_id, heartbeat_at, runtime_environment, provider_id, capability_level, "
            "execution_policy, model, reasoning, attempt_timeout_seconds, max_retries FROM executions "
            "WHERE status='RUNNING' ORDER BY started_at"
        ).fetchall()
        agents = [{
            "id": row["execution_id"], "role": "worker", "execution_kind": "WORKER", "status": "RUNNING",
            "current_task_id": row["task_id"], "last_seen_at": row["heartbeat_at"],
            "capability_level": row["capability_level"],
            "execution_policy": row["execution_policy"],
            "execution_profile": legacy_profile_for(row["capability_level"], row["execution_policy"]),
            "runtime_environment": row["runtime_environment"], "provider_id": row["provider_id"],
            "execution_config": {
                "model": row["model"], "reasoning": row["reasoning"],
                "attempt_timeout_seconds": row["attempt_timeout_seconds"],
                "max_retries": row["max_retries"],
            },
            "summary": f"Concurrent SQLite worker · {row['runtime_environment']} · {row['capability_level']}",
        } for row in executions]
    else:
        executions = database.execute(
            "SELECT e.execution_id, e.task_id, e.heartbeat_at, t.execution_profile, t.runtime_environment "
            "FROM executions e JOIN tasks t ON t.id=e.task_id "
            "WHERE e.status='RUNNING' ORDER BY e.started_at"
        ).fetchall()
        agents = []
        for row in executions:
            runtime_environment = (
                "self_hosted_agent" if row["runtime_environment"] == "deepseek" else row["runtime_environment"]
            )
            agents.append({
                "id": row["execution_id"], "role": "worker", "execution_kind": "WORKER", "status": "RUNNING",
                "current_task_id": row["task_id"], "last_seen_at": row["heartbeat_at"],
                "capability_level": LEGACY_PROFILE_TO_CAPABILITY[row["execution_profile"]],
                "execution_policy": "manual" if row["execution_profile"] == "exceptional" else "automatic",
                "execution_profile": row["execution_profile"],
                "runtime_environment": runtime_environment,
                "provider_id": "deepseek" if row["runtime_environment"] == "deepseek" else None,
                "summary": f"Concurrent SQLite worker · {runtime_environment} · {row['execution_profile']}",
            })
    planners: list[dict[str, Any]] = []
    if uses_preflight_schema(database):
        planners = [dict(row) for row in database.execute(
            "SELECT execution_id AS id, task_id AS current_task_id, execution_kind, status, "
            "started_at, heartbeat_at AS last_seen_at, lease_expires_at, attempt_deadline_at "
            "FROM preflight_executions WHERE status='INSPECTING' ORDER BY started_at"
        ).fetchall()]
    recoveries: list[dict[str, Any]] = []
    if uses_recovery_schema(database):
        rows = database.execute(
            "SELECT e.execution_id, e.task_id, e.status, e.finished_at, e.termination_reason, "
            "e.runtime_environment, e.provider_id, e.capability_level, e.execution_policy, "
            "e.recovered_at, e.recovery_action, l.scope_key, l.status AS scope_status, "
            "l.quarantined_at, l.quarantine_reason FROM executions e "
            "LEFT JOIN scope_locks l ON l.execution_id=e.execution_id "
            "WHERE e.recovery_required=1 ORDER BY e.started_at, l.scope_key"
        ).fetchall()
        by_execution: dict[str, dict[str, Any]] = {}
        for row in rows:
            recovery = by_execution.setdefault(
                row["execution_id"],
                {
                    "execution_id": row["execution_id"],
                    "task_id": row["task_id"],
                    "execution_status": row["status"],
                    "termination_reason": row["termination_reason"],
                    "runtime_environment": row["runtime_environment"],
                    "provider_id": row["provider_id"],
                    "capability_level": row["capability_level"],
                    "execution_policy": row["execution_policy"],
                    "finished_at": row["finished_at"],
                    "recovered_at": row["recovered_at"],
                    "recovery_action": row["recovery_action"],
                    "scope_status": row["scope_status"],
                    "quarantined_at": row["quarantined_at"],
                    "quarantine_reason": row["quarantine_reason"],
                    "scope_keys": [],
                },
            )
            if row["scope_key"] is not None:
                recovery["scope_keys"].append(row["scope_key"])
        recoveries = list(by_execution.values())
    updated = database.execute("SELECT max(updated_at) FROM tasks").fetchone()[0] or now_shanghai()
    workspace = value["workspace"]
    return {
        "schema_version": schema_version(database),
        "workspace": {
            "name": workspace["name"], "timezone": workspace["timezone"],
            "revision": current_revision(database), "updated_at": updated,
            "writer": "sqlite-task-store", "task_root": workspace["task_root"],
            "project_registry": workspace["project_registry"],
        },
        "settings": value["task_execution"],
        "agents": agents,
        "planners": planners,
        "planner_settings": value.get("planner", {}),
        "recoveries": recoveries,
        "tasks": all_tasks(database),
        "services": [],
        "health_events": [],
        "projects": configured_projects(value),
    }


def bump_revision(database: sqlite3.Connection, writer: str) -> int:
    del writer
    return current_revision(database)


def validate_database(database: sqlite3.Connection, config: dict[str, Any] | None = None) -> dict[str, Any]:
    value = config or load_initialization_config()
    errors: list[str] = []
    version = schema_version(database)
    if version != SCHEMA_VERSION:
        errors.append(f"schema_version={version}, expected={SCHEMA_VERSION}")
    foreign_keys = database.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        errors.append(f"foreign_key_check={len(foreign_keys)}")
    integrity = database.execute("PRAGMA quick_check").fetchone()[0]
    if integrity != "ok":
        errors.append(f"quick_check={integrity}")
    actual_tables = {
        row[0] for row in database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    unexpected = sorted(actual_tables - ALLOWED_TABLES)
    missing = sorted(ALLOWED_TABLES - actual_tables)
    if unexpected:
        errors.append("数据库包含非任务表: " + ",".join(unexpected))
    if missing:
        errors.append("数据库缺少任务表: " + ",".join(missing))
    if uses_result_diagnostic_schema(database):
        invalid_diagnostics: list[str] = []
        for row in database.execute(
            "SELECT id, result_diagnostic_json FROM tasks WHERE result_diagnostic_json IS NOT NULL"
        ):
            try:
                load_result_diagnostic(row["result_diagnostic_json"])
            except LoopError:
                invalid_diagnostics.append(row["id"])
        if invalid_diagnostics:
            errors.append("任务结果诊断无效: " + ",".join(invalid_diagnostics))
    if uses_preflight_schema(database) and not uses_hybrid_scope_schema(database):
        errors.append("当前 Schema 3.7.0 尚未迁移到混合 scope 锁结构")
    invalid_confirmed = database.execute(
        """SELECT t.id FROM tasks t WHERE t.status='CONFIRMED' AND NOT EXISTS (
          SELECT 1 FROM task_history h WHERE h.task_id=t.id AND h.to_status='CONFIRMED' AND h.from_status='SUCCEEDED'
        )"""
    ).fetchall()
    if invalid_confirmed:
        errors.append("CONFIRMED 缺少 SUCCEEDED 转入历史: " + ",".join(row[0] for row in invalid_confirmed))
    invalid_archived: list[str] = []
    for row in database.execute("SELECT id, archived_at FROM tasks WHERE archived_at IS NOT NULL"):
        try:
            parsed = datetime.fromisoformat(row["archived_at"])
            if parsed.utcoffset() is None:
                raise ValueError("missing timezone")
        except (TypeError, ValueError):
            invalid_archived.append(row["id"])
    if invalid_archived:
        errors.append("archived_at 不是带时区 ISO 8601: " + ",".join(invalid_archived))
    invalid_priorities = database.execute(
        "SELECT id FROM tasks WHERE priority NOT IN ('blocker','critical','high','medium','low')"
    ).fetchall()
    if invalid_priorities:
        errors.append("任务优先级无效: " + ",".join(row[0] for row in invalid_priorities))
    capability_schema = uses_capability_schema(database)
    if capability_schema:
        invalid_capabilities = database.execute(
            "SELECT id FROM tasks WHERE capability_level NOT IN ('L1','L2','L3','L4','L5')"
        ).fetchall()
        if invalid_capabilities:
            errors.append("任务能力等级无效: " + ",".join(row[0] for row in invalid_capabilities))
        invalid_policies = database.execute(
            "SELECT id FROM tasks WHERE execution_policy NOT IN ('automatic','manual')"
        ).fetchall()
        if invalid_policies:
            errors.append("任务执行策略无效: " + ",".join(row[0] for row in invalid_policies))
        invalid_provider_routes = database.execute(
            "SELECT id FROM tasks WHERE (runtime_environment='self_hosted_agent' AND "
            "(provider_id IS NULL OR length(trim(provider_id))=0)) OR "
            "(runtime_environment<>'self_hosted_agent' AND provider_id IS NOT NULL)"
        ).fetchall()
        if invalid_provider_routes:
            errors.append("任务 Provider 路由无效: " + ",".join(row[0] for row in invalid_provider_routes))
        invalid_snapshots = database.execute(
            "SELECT execution_id FROM executions WHERE model='' OR attempt_timeout_seconds<=0 OR max_retries<0"
        ).fetchall()
        if invalid_snapshots:
            errors.append("execution 配置快照无效: " + ",".join(row[0] for row in invalid_snapshots))
        if uses_preflight_schema(database):
            invalid_execution_kinds = database.execute(
                "SELECT execution_id FROM executions WHERE execution_kind<>'WORKER'"
            ).fetchall()
            if invalid_execution_kinds:
                errors.append("Worker execution_kind 无效: " + ",".join(row[0] for row in invalid_execution_kinds))
            invalid_preflight_states = database.execute(
                """SELECT id FROM tasks WHERE
                  (status='DRAFT' AND preflight_status NOT IN ('UNINSPECTED','INSPECTING')) OR
                  (status='NEEDS_REVIEW' AND preflight_status<>'FAILED') OR
                  (status IN ('PENDING','RUNNING','WAITING_CONFLICT','WAITING_HUMAN') AND preflight_status<>'READY') OR
                  (preflight_status='READY' AND (capability_level IS NULL OR lock_mode IS NULL OR
                    NOT EXISTS (SELECT 1 FROM task_scopes s WHERE s.task_id=tasks.id) OR
                    NOT EXISTS (SELECT 1 FROM task_technical_acceptance a WHERE a.task_id=tasks.id) OR
                    NOT EXISTS (SELECT 1 FROM task_preflight_evidence e WHERE e.task_id=tasks.id))) OR
                  (status IN ('DRAFT','NEEDS_REVIEW') AND (capability_level IS NOT NULL OR lock_mode IS NOT NULL)) OR
                  (preflight_status='INSPECTING' AND (preflight_execution_id IS NULL OR NOT EXISTS (
                    SELECT 1 FROM preflight_executions p WHERE p.execution_id=tasks.preflight_execution_id
                    AND p.task_id=tasks.id AND p.status='INSPECTING'))) OR
                  (preflight_status<>'INSPECTING' AND preflight_execution_id IS NOT NULL)"""
            ).fetchall()
            if invalid_preflight_states:
                errors.append("任务预检状态无效: " + ",".join(row[0] for row in invalid_preflight_states))
            invalid_planners = database.execute(
                """SELECT p.execution_id FROM preflight_executions p LEFT JOIN tasks t ON t.id=p.task_id
                WHERE p.execution_kind<>'PLANNER' OR
                  (p.status='INSPECTING' AND (t.id IS NULL OR t.status<>'DRAFT' OR
                    t.preflight_status<>'INSPECTING' OR t.preflight_execution_id<>p.execution_id)) OR
                  (p.status<>'INSPECTING' AND p.finished_at IS NULL)"""
            ).fetchall()
            if invalid_planners:
                errors.append("Planner execution 状态无效: " + ",".join(row[0] for row in invalid_planners))
        if uses_recovery_schema(database):
            invalid_recoveries = database.execute(
                "SELECT execution_id FROM executions WHERE "
                "(status IN ('STALLED','TIMED_OUT') AND (recovery_required<>1 OR finished_at IS NULL "
                "OR termination_reason IS NULL)) OR "
                "(status IN ('RUNNING','FINISHED','EXPIRED') AND recovery_required=1)"
            ).fetchall()
            if invalid_recoveries:
                errors.append(
                    "execution 恢复状态无效: " + ",".join(row[0] for row in invalid_recoveries)
                )
    else:
        invalid_profiles = database.execute(
            "SELECT id FROM tasks WHERE execution_profile NOT IN "
            "('routine','standard','advanced','deep','complex','exceptional')"
        ).fetchall()
        if invalid_profiles:
            errors.append("任务执行档位无效: " + ",".join(row[0] for row in invalid_profiles))
    runtime_values = (
        "('codex_automation','codex_cli','self_hosted_agent')" if capability_schema
        else "('codex_automation','codex_cli','deepseek')"
    )
    invalid_runtime_environments = database.execute(
        f"SELECT id FROM tasks WHERE runtime_environment NOT IN {runtime_values}"
    ).fetchall()
    if invalid_runtime_environments:
        errors.append("任务运行环境无效: " + ",".join(row[0] for row in invalid_runtime_environments))
    dependency_cycle = dependency_cycle_path(database)
    if dependency_cycle:
        errors.append("循环依赖: " + " -> ".join(dependency_cycle))
    active = database.execute("SELECT count(*) FROM executions WHERE status='RUNNING'").fetchone()[0]
    maximum = global_parallel_limit(value)
    if active > maximum:
        errors.append(f"active_executions={active} exceeds global maximum={maximum}")
    for platform in CANONICAL_RUNTIME_ENVIRONMENTS:
        if capability_schema:
            platform_active = database.execute(
                "SELECT count(*) FROM executions WHERE status='RUNNING' AND runtime_environment=?",
                (platform,),
            ).fetchone()[0]
        else:
            legacy_platform = "deepseek" if platform == "self_hosted_agent" else platform
            platform_active = database.execute(
                "SELECT count(*) FROM executions e JOIN tasks t ON t.id=e.task_id "
                "WHERE e.status='RUNNING' AND t.runtime_environment=?",
                (legacy_platform,),
            ).fetchone()[0]
        platform_maximum = platform_parallel_limit(platform, value)
        if platform_active > platform_maximum:
            errors.append(
                f"platform={platform} active_executions={platform_active} exceeds maximum={platform_maximum}"
            )
    orphan_running = database.execute(
        """SELECT t.id FROM tasks t WHERE t.status='RUNNING' AND NOT EXISTS (
          SELECT 1 FROM executions e WHERE e.task_id=t.id AND e.status='RUNNING'
        )"""
    ).fetchall()
    if orphan_running:
        errors.append("RUNNING 任务缺少活动 execution: " + ",".join(row[0] for row in orphan_running))
    if uses_recovery_schema(database):
        mismatched_locks = database.execute(
            """SELECT l.scope_key FROM scope_locks l
            LEFT JOIN executions e ON e.execution_id=l.execution_id
            LEFT JOIN tasks t ON t.id=l.task_id
            WHERE e.execution_id IS NULL OR e.task_id<>l.task_id OR t.id IS NULL OR
              (l.status='ACTIVE' AND (e.status<>'RUNNING' OR t.status<>'RUNNING')) OR
              (l.status='QUARANTINED' AND (
                e.status NOT IN ('STALLED','TIMED_OUT') OR e.recovery_required<>1 OR
                t.status<>'WAITING_HUMAN' OR l.quarantined_at IS NULL OR l.quarantine_reason IS NULL
              ))"""
        ).fetchall()
    else:
        mismatched_locks = database.execute(
            """SELECT l.scope_key FROM scope_locks l LEFT JOIN executions e ON e.execution_id=l.execution_id
            WHERE e.execution_id IS NULL OR e.status<>'RUNNING' OR e.task_id<>l.task_id"""
        ).fetchall()
    if mismatched_locks:
        errors.append("scope 锁与 execution 生命周期不一致: " + ",".join(row[0] for row in mismatched_locks))
    invalid_scope_keys: list[str] = []
    for row in database.execute("SELECT task_id, scope_key FROM task_scopes ORDER BY task_id, ordinal"):
        try:
            parse_scope_key(row["scope_key"])
        except LoopError:
            invalid_scope_keys.append(f"{row['task_id']}:{row['scope_key']}")
    for row in database.execute("SELECT task_id, scope_key FROM scope_locks ORDER BY task_id, scope_key"):
        try:
            parse_scope_key(row["scope_key"])
        except LoopError:
            invalid_scope_keys.append(f"{row['task_id']}:{row['scope_key']}")
    if invalid_scope_keys:
        errors.append("scope_key 无效: " + ",".join(invalid_scope_keys))
    invalid_scope_modes = database.execute(
        "SELECT t.id, s.scope_key FROM tasks t JOIN task_scopes s ON s.task_id=t.id "
        "WHERE t.preflight_status='READY' AND ("
        "(t.lock_mode='project' AND s.scope_key NOT LIKE 'project:%' "
        "AND s.scope_key NOT LIKE 'external:%') OR "
        "(t.lock_mode='module' AND s.scope_key NOT LIKE 'module:%') OR "
        "(t.lock_mode='file' AND s.scope_key NOT LIKE 'file:%')) "
        "ORDER BY t.id, s.ordinal"
    ).fetchall() if uses_preflight_schema(database) else []
    if invalid_scope_modes:
        errors.append(
            "任务 lock_mode 与 scope_key 不一致: "
            + ",".join(f"{row['id']}:{row['scope_key']}" for row in invalid_scope_modes)
        )
    locks_without_scope = database.execute(
        "SELECT l.execution_id, l.scope_key FROM scope_locks l WHERE NOT EXISTS ("
        "SELECT 1 FROM task_scopes s WHERE s.task_id=l.task_id AND s.scope_key=l.scope_key) "
        "ORDER BY l.execution_id, l.scope_key"
    ).fetchall()
    if locks_without_scope:
        errors.append(
            "scope 锁缺少任务范围凭证: "
            + ",".join(f"{row['execution_id']}:{row['scope_key']}" for row in locks_without_scope)
        )
    active_locks = [dict(row) for row in database.execute(
        "SELECT scope_key, task_id, execution_id FROM scope_locks ORDER BY scope_key"
    ).fetchall()]
    overlapping_locks: list[str] = []
    for index, left in enumerate(active_locks):
        for right in active_locks[index + 1:]:
            if left["execution_id"] == right["execution_id"]:
                continue
            if scope_keys_conflict(left["scope_key"], right["scope_key"]):
                overlapping_locks.append(
                    f"{left['execution_id']}:{left['scope_key']}<->{right['execution_id']}:{right['scope_key']}"
                )
    if overlapping_locks:
        errors.append("活动 scope 锁互相冲突: " + ",".join(overlapping_locks))
    return {
        "ok": not errors, "schema_version": version,
        "tasks": database.execute("SELECT count(*) FROM tasks").fetchone()[0],
        "active_executions": active, "global_max_active_executions": maximum,
        "tables": sorted(actual_tables), "errors": errors,
    }
