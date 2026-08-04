PRAGMA foreign_keys = ON;
PRAGMA user_version = 30400;

CREATE TABLE IF NOT EXISTS tasks (
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
);

CREATE TABLE IF NOT EXISTS task_dependencies (
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  dependency_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
  PRIMARY KEY (task_id, dependency_id),
  CHECK (task_id <> dependency_id)
);

CREATE TABLE IF NOT EXISTS task_scopes (
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  scope TEXT NOT NULL,
  scope_key TEXT NOT NULL,
  PRIMARY KEY (task_id, ordinal),
  UNIQUE (task_id, scope)
);

CREATE INDEX IF NOT EXISTS idx_task_scopes_key ON task_scopes(scope_key);

CREATE TABLE IF NOT EXISTS task_acceptance (
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  text TEXT NOT NULL,
  PRIMARY KEY (task_id, ordinal)
);

CREATE TABLE IF NOT EXISTS task_completed_items (
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  text TEXT NOT NULL,
  PRIMARY KEY (task_id, ordinal)
);

CREATE TABLE IF NOT EXISTS task_verifications (
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  text TEXT NOT NULL,
  PRIMARY KEY (task_id, ordinal)
);

CREATE TABLE IF NOT EXISTS task_attachments (
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  path TEXT NOT NULL,
  sha256 TEXT,
  role TEXT NOT NULL CHECK (role IN ('source', 'derived', 'result')),
  saved_at TEXT NOT NULL,
  PRIMARY KEY (task_id, ordinal)
);

CREATE TABLE IF NOT EXISTS task_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  at TEXT NOT NULL,
  from_status TEXT,
  to_status TEXT NOT NULL,
  actor TEXT NOT NULL,
  reason TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_history_task ON task_history(task_id, id);

CREATE TABLE IF NOT EXISTS executions (
  execution_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK (status IN ('RUNNING', 'FINISHED', 'EXPIRED')),
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
  CHECK (
    (runtime_environment = 'self_hosted_agent' AND provider_id IS NOT NULL AND length(trim(provider_id)) > 0)
    OR (runtime_environment <> 'self_hosted_agent' AND provider_id IS NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_executions_active ON executions(status, lease_expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_executions_one_active_task
  ON executions(task_id) WHERE status='RUNNING';

CREATE TABLE IF NOT EXISTS scope_locks (
  scope_key TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  execution_id TEXT NOT NULL REFERENCES executions(execution_id) ON DELETE CASCADE,
  acquired_at TEXT NOT NULL,
  lease_expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_conflicts (
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  scope_key TEXT NOT NULL,
  blocker_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  blocker_execution_id TEXT NOT NULL,
  detected_at TEXT NOT NULL,
  PRIMARY KEY (task_id, scope_key, blocker_execution_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_queue
  ON tasks(status, runtime_environment, provider_id, capability_level, execution_policy, priority, created_at, id);

CREATE INDEX IF NOT EXISTS idx_tasks_archived
  ON tasks(archived_at, status, updated_at);
