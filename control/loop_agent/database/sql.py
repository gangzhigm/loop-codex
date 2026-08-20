"""Schema 创建和迁移使用的标准 SQL。

这些语句刻意保持显式定义，不通过 ORM 生成。排查初始化或迁移失败时，Operator 可以直接
将它们与 SQLite 元数据进行比较。
"""

# 中文排查：DDL 保持显式文本，便于将本文件与 sqlite_master 和 schemas/loop-agent.sql 对照。
# 建表或迁移失败时先定位具体 SQL 块，再检查 schema.py 调用顺序和当前 user_version。

TASKS_TABLE_SQL = """
CREATE TABLE tasks_new (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN (
    'DRAFT', 'NEEDS_REVIEW', 'PENDING', 'QUEUED', 'RUNNING', 'WAITING_CONFLICT', 'WAITING_HUMAN',
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
    'UNINSPECTED', 'QUEUED', 'INSPECTING', 'READY', 'FAILED'
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


EXECUTIONS_TABLE_SQL = """
CREATE TABLE executions_new (
  execution_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK (status IN (
    'QUEUED', 'RUNNING', 'FINISHED', 'EXPIRED', 'STALLED', 'TIMED_OUT'
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
  status TEXT NOT NULL CHECK (status IN ('QUEUED', 'INSPECTING', 'FINISHED', 'FAILED', 'TIMED_OUT')),
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
