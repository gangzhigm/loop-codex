PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
  path TEXT PRIMARY KEY,
  description TEXT NOT NULL DEFAULT '',
  exists_on_disk INTEGER NOT NULL DEFAULT 1 CHECK (exists_on_disk IN (0, 1)),
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN (
    'DRAFT', 'PENDING', 'RUNNING', 'WAITING_CONFLICT', 'WAITING_HUMAN',
    'SUCCEEDED', 'CONFIRMED', 'FAILED', 'CANCELLED'
  )),
  priority TEXT NOT NULL CHECK (priority IN ('critical', 'high', 'medium', 'low')),
  assigned_agent TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  updated_at TEXT NOT NULL,
  heartbeat_at TEXT,
  completed_at TEXT,
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
  row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version >= 1)
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
  outcome TEXT
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

CREATE TABLE IF NOT EXISTS change_requests (
  id TEXT PRIMARY KEY,
  action TEXT NOT NULL,
  task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
  reason TEXT NOT NULL,
  requested_at TEXT NOT NULL,
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS health_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  component TEXT NOT NULL,
  status TEXT NOT NULL,
  message TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS service_state (
  component TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  pid INTEGER,
  checked_at TEXT NOT NULL,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_tasks_queue
  ON tasks(status, priority, created_at, id);
