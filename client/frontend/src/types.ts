export const TASK_SCHEMA_VERSION = "3.9.0";

export type TaskStatus =
  | "DRAFT"
  | "NEEDS_REVIEW"
  | "PENDING"
  | "QUEUED"
  | "CLAIMED"
  | "RUNNING"
  | "WAITING_CONFLICT"
  | "WAITING_HUMAN"
  | "BLOCKED"
  | "STALLED"
  | "SUCCEEDED"
  | "CONFIRMED"
  | "FAILED"
  | "CANCELLED";

export type Priority = "blocker" | "critical" | "high" | "medium" | "low";
export type CapabilityLevel = "L1" | "L2" | "L3" | "L4" | "L5";
export type PrimaryFilter = "all" | "draft" | "review" | "pending" | "queued" | "active" | "closed" | "archived";

export interface TaskProgress {
  percent: number;
  summary?: string;
  completed?: string[];
}

export interface TaskResult {
  summary?: string | null;
  error?: string | null;
  verification?: string[];
  diagnostic?: Record<string, unknown> | null;
}

export interface TaskDefinition {
  description?: string;
  acceptance?: string[];
}

export interface PlannerSupplement {
  preflight_status?: Task["preflight_status"];
  execution_id?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  failure?: string | null;
  capability_level?: CapabilityLevel | null;
  lock_mode?: string | null;
  scope?: string[];
  technical_acceptance?: string[];
  evidence?: string[];
  split_suggestions?: SplitSuggestion[];
  [key: string]: unknown;
}

export interface BlockingScope {
  blocker_task_id: string;
  requested_scope_key: string;
  blocker_execution_id?: string | null;
  scope_key?: string;
  blocker_lock_status?: string;
  detected_at?: string | null;
  [key: string]: unknown;
}

export interface SplitSuggestion {
  reason: string;
  tasks: Array<{
    id: string;
    title: string;
    description: string;
    scope: string[];
    capability_level: CapabilityLevel;
    depends_on: string[];
    parallel_with: string[];
  }>;
}

export interface Attachment {
  path: string;
  role?: string;
  mime_type?: string;
  [key: string]: unknown;
}

export interface TaskHistoryItem {
  at?: string;
  from?: string | null;
  to?: string;
  reason?: string;
  actor?: string;
  [key: string]: unknown;
}

export interface Task {
  id: string;
  title: string;
  description?: string;
  status: TaskStatus;
  priority: Priority;
  row_version: number;
  preflight_status: "UNINSPECTED" | "QUEUED" | "INSPECTING" | "READY" | "FAILED";
  capability_level: CapabilityLevel | null;
  estimated_capability_level: CapabilityLevel;
  runtime_environment: string;
  provider_id: string | null;
  execution_policy?: string;
  lock_mode?: string | null;
  scope: string[];
  scope_keys: string[];
  scope_hint?: string[];
  depends_on?: string[];
  blocked_by_task_ids: string[];
  blocked_scopes: string[];
  blocked_scope_keys: string[];
  blocking_scopes: BlockingScope[];
  scope_queue_position: number | null;
  operator_definition: TaskDefinition;
  planner_supplement: PlannerSupplement;
  progress: TaskProgress;
  result: TaskResult;
  assigned_agent?: string | null;
  heartbeat_at?: string | null;
  attempt?: number;
  archived_at?: string | null;
  created_at?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  updated_at?: string | null;
  human_intervention?: {
    required?: boolean;
    question?: string | null;
    options?: string[];
    requested_at?: string | null;
    responded_at?: string | null;
    response?: string | null;
  } | null;
  attachments?: Attachment[];
  history?: TaskHistoryItem[];
  [key: string]: unknown;
}

export interface RuntimeService {
  component: string;
  status: string;
  message?: string;
  mode?: string;
  pid?: number | null;
  worker_pid?: number | null;
  task_id?: string | null;
  execution_id?: string | null;
  started_at?: string | null;
  checked_at?: string | null;
  active_count?: number;
  observed_count?: number;
  heartbeat?: { checked_at?: string | null } | null;
  [key: string]: unknown;
}

export interface DatabaseService {
  status: string;
  schema_version?: number | string;
  checked_at?: string | null;
  message?: string;
}

export interface AgentExecution {
  id?: string;
  runtime_environment: string;
  provider_id: string | null;
  capability_level: CapabilityLevel;
  [key: string]: unknown;
}

export interface RecoveryRecord {
  execution_id: string;
  task_id: string;
  execution_status: "STALLED" | "TIMED_OUT";
  scope_status: "QUARANTINED";
  scope_keys: string[];
  quarantine_reason?: string;
  termination_reason?: string;
}

export interface ExecutionCapabilityConfig {
  model?: string;
  reasoning?: string;
  attempt_timeout_seconds?: number;
  max_retries?: number;
}

export interface RuntimeConfig {
  dashboard?: { poll_interval_ms?: number };
  runtime_environments: Record<string, { name: string }>;
  execution_profiles?: Record<string, {
    capabilities?: Partial<Record<CapabilityLevel, ExecutionCapabilityConfig>>;
    providers?: Record<string, { capabilities?: Partial<Record<CapabilityLevel, ExecutionCapabilityConfig>> }>;
  }>;
  [key: string]: unknown;
}

export interface DashboardState {
  schema_version: string;
  workspace: { revision: number; [key: string]: unknown };
  tasks: Task[];
  agents: AgentExecution[];
  recoveries: RecoveryRecord[];
  services: RuntimeService[];
  database: DatabaseService;
  service_control: { supervisor: boolean; scheduler: boolean; runner: boolean };
  scheduler_control: { planner_automation: boolean; dispatcher_automation: boolean };
  runtime_config: RuntimeConfig;
  projects?: Array<{ path?: string; [key: string]: unknown }>;
  settings?: {
    global_max_active_executions?: number;
    stalled_after_seconds?: number;
    profile_parallel_limits?: Record<string, number>;
    [key: string]: unknown;
  };
  [key: string]: unknown;
}

export interface SecretProvider {
  provider_id: string;
  configured: boolean | null;
  status: string;
  backend?: string;
  persistent?: boolean;
  last_validated_at?: string | null;
  validation_scope?: string | null;
  repair?: string | null;
}

export interface TaskFilters {
  primary: PrimaryFilter;
  status: string;
  priority: string;
  environment: string;
  project: string;
  capability: string;
  query: string;
}
