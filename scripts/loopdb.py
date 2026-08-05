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
SCHEMA_VERSION = "3.5.0"
SCHEMA_USER_VERSION = 30500
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
EXECUTION_POLICIES = ("automatic", "manual")
CANONICAL_RUNTIME_ENVIRONMENTS = ("codex_automation", "codex_cli", "self_hosted_agent")
LEGACY_RUNTIME_ENVIRONMENTS = ("codex_automation", "codex_cli", "deepseek")
# Remove these input aliases after production is Schema 3.4.0 and every launcher uses capability arguments.
RUNTIME_ENVIRONMENTS = CANONICAL_RUNTIME_ENVIRONMENTS + ("deepseek",)
CLAIM_RUNTIME_ENVIRONMENTS = RUNTIME_ENVIRONMENTS
LEGACY_PROFILE_TO_CAPABILITY = {
    "routine": "L1", "standard": "L2", "advanced": "L3",
    "deep": "L4", "complex": "L5", "exceptional": "L5",
}
FORBIDDEN_SCOPE_ROOTS = {"$CODEX_HOME", ".reasonix", ".env"}
ALLOWED_TABLES = {
    "tasks",
    "task_dependencies",
    "task_scopes",
    "task_acceptance",
    "task_completed_items",
    "task_verifications",
    "task_attachments",
    "task_history",
    "executions",
    "scope_locks",
    "task_conflicts",
}

TASKS_TABLE_SQL = """
CREATE TABLE tasks_new (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN (
    'DRAFT', 'PENDING', 'RUNNING', 'WAITING_CONFLICT', 'WAITING_HUMAN',
    'SUCCEEDED', 'CONFIRMED', 'FAILED', 'CANCELLED'
  )),
  priority TEXT NOT NULL CHECK (priority IN ('blocker', 'critical', 'high', 'medium', 'low')),
  capability_level TEXT NOT NULL DEFAULT 'L2' CHECK (capability_level IN (
    'L1', 'L2', 'L3', 'L4', 'L5'
  )),
  runtime_environment TEXT NOT NULL CHECK (runtime_environment IN (
    'codex_automation', 'codex_cli', 'self_hosted_agent'
  )),
  provider_id TEXT,
  execution_policy TEXT NOT NULL DEFAULT 'automatic' CHECK (execution_policy IN (
    'automatic', 'manual'
  )),
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


class LoopError(RuntimeError):
    pass


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
    self_hosted_agent = config.get("self_hosted_agent") or {}
    deepseek = config.get("deepseek") or {}
    priority_policy = config.get("priority_policy") or {}
    dashboard = config.get("dashboard") or {}
    automations = config.get("automations") or {}
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
        config.get("config_version") == "4.0.0"
        and workspace.get("timezone") == "Asia/Shanghai"
        and isinstance(workspace.get("name"), str)
        and isinstance(workspace.get("task_root"), str)
        and isinstance(workspace.get("project_registry"), str)
        and database.get("path") == "data/loop-agent.sqlite3"
        and database.get("schema_version") == SCHEMA_VERSION
        and prompts.get("operator") == "prompts/operator.md"
        and prompts.get("worker") == "prompts/worker.md"
        and (BASE_DIR / prompts["operator"]).is_file()
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
        and isinstance(self_hosted_agent.get("max_steps"), int)
        and 1 <= self_hosted_agent["max_steps"] <= 200
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
        "from": "3.4.0",
        "to": SCHEMA_VERSION,
        "migrated": True,
        "active_executions_preserved": active,
        "scope_locks_preserved": len(lock_rows),
        "quarantines_created": 0,
    }


def migrate_schema(database: sqlite3.Connection) -> dict[str, Any]:
    current = int(database.execute("PRAGMA user_version").fetchone()[0])
    if current == SCHEMA_USER_VERSION:
        return {
            "from": SCHEMA_VERSION, "to": SCHEMA_VERSION, "migrated": False,
            "archived": 0, "tasks_mapped": 0, "executions_snapshotted": 0,
        }
    if current == RECOVERY_SCHEMA_USER_VERSION:
        return _migrate_recovery_schema(database)
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
        for row in task_rows:
            route = normalized_tasks[row["id"]]
            database.execute(
                """INSERT INTO tasks_new(
                  id, title, description, status, priority, capability_level, runtime_environment,
                  provider_id, execution_policy, assigned_agent, created_at, started_at, updated_at,
                  heartbeat_at, completed_at, archived_at, attempt, progress_percent, progress_summary,
                  progress_next_step, result_summary, result_error, human_required, human_question,
                  human_options_json, human_requested_at, human_responded_at, human_response, row_version
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["id"], row["title"], row.get("description", ""), row["status"], row["priority"],
                    route["capability_level"], route["runtime_environment"], route["provider_id"],
                    route["execution_policy"], row.get("assigned_agent"), row["created_at"],
                    row.get("started_at"), row["updated_at"], row.get("heartbeat_at"),
                    row.get("completed_at"), row.get("archived_at"), row.get("attempt", 0),
                    row.get("progress_percent", 0), row.get("progress_summary", ""),
                    row.get("progress_next_step"), row.get("result_summary"), row.get("result_error"),
                    row.get("human_required", 0), row.get("human_question"),
                    row.get("human_options_json", "[]"), row.get("human_requested_at"),
                    row.get("human_responded_at"), row.get("human_response"), row.get("row_version", 1),
                ),
            )
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
            "CREATE INDEX idx_tasks_queue ON tasks(status, runtime_environment, provider_id, capability_level, "
            "execution_policy, priority, created_at, id)"
        )
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


def resolve_scope_key(scope: str, project_paths: Iterable[str] | None = None) -> str:
    normalized = scope.replace("\\", "/").strip()
    if not normalized or ".." in normalized.split("/"):
        raise LoopError(f"不安全的 scope: {scope}")
    if normalized.upper().startswith("OSS:"):
        return f"external:{normalized}"
    normalized = normalized.strip("/")
    if normalized.split("/", 1)[0] in FORBIDDEN_SCOPE_ROOTS:
        raise LoopError(f"禁止的 scope: {scope}")
    paths = list(project_paths) if project_paths is not None else [item["path"] for item in configured_projects()]
    for project in sorted(paths, key=len, reverse=True):
        project = project.replace("\\", "/").strip("/")
        if normalized == project or normalized.startswith(f"{project}/"):
            return f"project:{project}"
    raise LoopError(f"scope 未匹配项目清单: {scope}")


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
    execution_policy = task.get("execution_policy")
    runtime_environment = task.get("runtime_environment")
    provider_id = task.get("provider_id")
    if priority not in PRIORITIES:
        raise LoopError(f"任务优先级无效: {priority}")
    if execution_profile is not None and execution_profile not in EXECUTION_PROFILES:
        raise LoopError(f"执行档位无效: {execution_profile}")
    if capability_level is None:
        capability_level = LEGACY_PROFILE_TO_CAPABILITY[execution_profile or "standard"]
    if capability_level not in CAPABILITY_LEVELS:
        raise LoopError(f"任务能力等级无效: {capability_level}")
    if execution_policy is None:
        execution_policy = "manual" if execution_profile == "exceptional" else "automatic"
    if execution_policy not in EXECUTION_POLICIES:
        raise LoopError(f"执行策略无效: {execution_policy}")
    if execution_profile is not None and legacy_profile_for(capability_level, execution_policy) != execution_profile:
        raise LoopError("旧 execution_profile 与 capability_level/execution_policy 不一致")
    runtime_environment, provider_id = normalize_execution_target(runtime_environment, provider_id)
    resolve_execution_profile(runtime_environment, provider_id, capability_level)
    stamp = task.get("created_at") or now_shanghai()
    progress = task.get("progress") or {}
    result = task.get("result") or {}
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
    if uses_capability_schema(database):
        database.execute(
            """INSERT INTO tasks(
              id, title, description, status, priority, capability_level, runtime_environment, provider_id,
              execution_policy, assigned_agent, created_at, started_at, updated_at, heartbeat_at,
              completed_at, archived_at, attempt, progress_percent, progress_summary, progress_next_step,
              result_summary, result_error, human_required, human_question, human_options_json,
              human_requested_at, human_responded_at, human_response, row_version
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id, str(task.get("title") or task_id), str(task.get("description") or ""), status,
                priority, capability_level, runtime_environment, provider_id, execution_policy, *common_tail,
            ),
        )
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
    for index, scope in enumerate(task.get("scope") or []):
        database.execute(
            "INSERT INTO task_scopes(task_id, ordinal, scope, scope_key) VALUES(?, ?, ?, ?)",
            (task_id, index, scope, resolve_scope_key(scope, project_paths)),
        )
    replace_ordered_text(database, "task_acceptance", task_id, task.get("acceptance") or [])
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
        execution_profile = legacy_profile_for(capability_level, execution_policy)
    else:
        execution_profile = row["execution_profile"]
        capability_level = LEGACY_PROFILE_TO_CAPABILITY[execution_profile]
        execution_policy = "manual" if execution_profile == "exceptional" else "automatic"
        legacy_environment = row["runtime_environment"]
        runtime_environment = "self_hosted_agent" if legacy_environment == "deepseek" else legacy_environment
        provider_id = "deepseek" if legacy_environment == "deepseek" else None
    return {
        "id": task_id, "title": row["title"], "description": row["description"],
        "status": row["status"], "priority": row["priority"],
        "capability_level": capability_level, "execution_policy": execution_policy,
        "provider_id": provider_id, "runtime_environment": runtime_environment,
        "execution_profile": execution_profile,
        "assigned_agent": row["assigned_agent"],
        "created_at": row["created_at"], "started_at": row["started_at"], "updated_at": row["updated_at"],
        "heartbeat_at": row["heartbeat_at"], "completed_at": row["completed_at"],
        "archived_at": row["archived_at"], "attempt": row["attempt"],
        "depends_on": task_children(database, task_id, "task_dependencies", "dependency_id", "dependency_id"),
        "scope": task_children(database, task_id, "task_scopes", "scope"),
        "scope_keys": task_children(database, task_id, "task_scopes", "scope_key"),
        "acceptance": task_children(database, task_id, "task_acceptance"),
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
    return task_versions + histories + executions


def state_payload(database: sqlite3.Connection, config: dict[str, Any] | None = None) -> dict[str, Any]:
    value = config or load_initialization_config()
    if uses_capability_schema(database):
        executions = database.execute(
            "SELECT execution_id, task_id, heartbeat_at, runtime_environment, provider_id, capability_level, "
            "execution_policy, model, reasoning, attempt_timeout_seconds, max_retries FROM executions "
            "WHERE status='RUNNING' ORDER BY started_at"
        ).fetchall()
        agents = [{
            "id": row["execution_id"], "role": "worker", "status": "RUNNING",
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
                "id": row["execution_id"], "role": "worker", "status": "RUNNING",
                "current_task_id": row["task_id"], "last_seen_at": row["heartbeat_at"],
                "capability_level": LEGACY_PROFILE_TO_CAPABILITY[row["execution_profile"]],
                "execution_policy": "manual" if row["execution_profile"] == "exceptional" else "automatic",
                "execution_profile": row["execution_profile"],
                "runtime_environment": runtime_environment,
                "provider_id": "deepseek" if row["runtime_environment"] == "deepseek" else None,
                "summary": f"Concurrent SQLite worker · {runtime_environment} · {row['execution_profile']}",
            })
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
    return {
        "ok": not errors, "schema_version": version,
        "tasks": database.execute("SELECT count(*) FROM tasks").fetchone()[0],
        "active_executions": active, "global_max_active_executions": maximum,
        "tables": sorted(actual_tables), "errors": errors,
    }
