import {
  CapabilityLevel,
  DashboardState,
  PrimaryFilter,
  RuntimeService,
  Task,
  TaskFilters,
  TaskStatus,
} from "./types";

export const STATUS_LABELS: Record<TaskStatus, string> = {
  DRAFT: "草稿",
  NEEDS_REVIEW: "需确认",
  PENDING: "待执行",
  QUEUED: "AI 队列中",
  CLAIMED: "已领取",
  RUNNING: "执行中",
  WAITING_CONFLICT: "等待冲突",
  WAITING_HUMAN: "等待人工",
  BLOCKED: "已阻塞",
  STALLED: "已卡顿",
  SUCCEEDED: "已完成",
  CONFIRMED: "已确认",
  FAILED: "失败",
  CANCELLED: "已取消",
};

export const PRIORITY_LABELS = { blocker: "阻断", critical: "紧急", high: "高", medium: "中", low: "低" } as const;
export const PREFLIGHT_LABELS = {
  UNINSPECTED: "未进入 Planner 队列",
  QUEUED: "Planner 队列中",
  INSPECTING: "Planner 处理中",
  READY: "预检完成",
  FAILED: "预检未通过",
} as const;
export const PRIORITY_ORDER = { blocker: 0, critical: 1, high: 2, medium: 3, low: 4 } as const;
export const CAPABILITY_LEVELS: CapabilityLevel[] = ["L1", "L2", "L3", "L4", "L5"];
export const REVIEW_STATUSES = new Set<TaskStatus>([
  "NEEDS_REVIEW",
  "WAITING_CONFLICT",
  "WAITING_HUMAN",
  "BLOCKED",
  "STALLED",
]);
export const ACTIVE_STATUSES = new Set<TaskStatus>(["CLAIMED", "RUNNING"]);
export const CLOSED_STATUSES = new Set<TaskStatus>(["SUCCEEDED", "CONFIRMED", "FAILED", "CANCELLED"]);

export const PRIMARY_FILTERS: Array<{ id: PrimaryFilter; label: string; help: string }> = [
  { id: "all", label: "全部任务", help: "包含已归档任务" },
  { id: "draft", label: "草稿", help: "DRAFT" },
  { id: "review", label: "待处理", help: "需确认、冲突、等待人工、阻塞或卡顿" },
  { id: "pending", label: "待执行", help: "PENDING" },
  { id: "queued", label: "AI 队列", help: "QUEUED" },
  { id: "active", label: "执行中", help: "CLAIMED、RUNNING" },
  { id: "closed", label: "已结束", help: "未归档终态任务" },
  { id: "archived", label: "已归档", help: "archived_at 已设置" },
];

export function formatDate(value?: string | null, includeDate = true): string {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间无效";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: includeDate ? "numeric" : undefined,
    month: includeDate ? "2-digit" : undefined,
    day: includeDate ? "2-digit" : undefined,
    hour: "2-digit",
    minute: "2-digit",
    second: includeDate ? undefined : "2-digit",
    hour12: false,
  }).format(date);
}

export function formatDuration(start?: string | null, end?: string | null): string {
  if (!start) return "--";
  const startTime = new Date(start).getTime();
  const endTime = end ? new Date(end).getTime() : Date.now();
  if (!Number.isFinite(startTime) || !Number.isFinite(endTime)) return "--";
  const seconds = Math.max(0, Math.floor((endTime - startTime) / 1000));
  if (seconds < 60) return `${seconds}秒`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}分钟`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}小时 ${minutes % 60}分`;
  return `${Math.floor(hours / 24)}天 ${hours % 24}小时`;
}

export function heartbeatAge(value?: string | null): string {
  if (!value) return "无 heartbeat";
  const milliseconds = Date.now() - new Date(value).getTime();
  if (!Number.isFinite(milliseconds)) return "时间无效";
  if (milliseconds < -1000) return "时间在未来";
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  if (seconds < 60) return `${seconds} 秒前`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分前`;
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? `${hours} 小时前` : `${Math.floor(hours / 24)} 天前`;
}

export function heartbeatTimestamp(service?: RuntimeService | null): string | null {
  return service?.heartbeat?.checked_at ?? service?.checked_at ?? null;
}

export function runtimeTone(status?: string): "healthy" | "active" | "warning" | "danger" | "neutral" {
  if (status === "HEALTHY") return "healthy";
  if (status === "RESTARTED") return "active";
  if (["RESTARTING", "STOPPING"].includes(status ?? "")) return "warning";
  if (["STOP_FAILED", "BLOCKED", "UNHEALTHY", "NEEDS_ATTENTION", "STALE", "PROCESS_MISSING", "IDENTITY_MISMATCH", "WORKER_MISSING", "INVALID_STATE", "UNAVAILABLE"].includes(status ?? "")) return "danger";
  return "neutral";
}

export function scopeKeyProject(key: string): string | null {
  if (key.startsWith("project:")) {
    const project = key.slice("project:".length);
    return project && !project.includes(":") ? project : null;
  }
  return /^(?:file|module):([^:]+)::(.+)$/.exec(key)?.[1] ?? null;
}

export function taskProjects(task: Task, state: DashboardState): string[] {
  const projects = new Map<string, string[]>();
  task.scope_keys.forEach((key, index) => {
    const project = scopeKeyProject(key);
    if (!project) return;
    const scopes = projects.get(project) ?? [];
    scopes.push(String(task.scope[index] ?? "").replaceAll("\\", "/"));
    projects.set(project, scopes);
  });
  if (!projects.size) {
    for (const hint of task.scope_hint ?? []) {
      const normalized = hint.replaceAll("\\", "/").replace(/^\.\//, "");
      const project = (state.projects ?? [])
        .map((item) => String(item.path ?? "").replaceAll("\\", "/").replace(/\/$/, ""))
        .filter((path) => path && (normalized === path || normalized.startsWith(`${path}/`)))
        .sort((left, right) => right.length - left.length)[0];
      if (project) projects.set(project, [...(projects.get(project) ?? []), normalized]);
    }
  }
  if (projects.size > 1) {
    const loopScopes = projects.get("local-agent-loop") ?? [];
    if (loopScopes.length && loopScopes.every((scope) => scope.startsWith("local-agent-loop/data/assets/"))) projects.delete("local-agent-loop");
  }
  return [...projects.keys()];
}

export function matchesPrimary(task: Task, filter: PrimaryFilter): boolean {
  if (filter === "all") return true;
  if (filter === "archived") return Boolean(task.archived_at);
  if (task.archived_at) return false;
  if (filter === "draft") return task.status === "DRAFT";
  if (filter === "review") return REVIEW_STATUSES.has(task.status);
  if (filter === "pending") return task.status === "PENDING";
  if (filter === "queued") return task.status === "QUEUED";
  if (filter === "active") return ACTIVE_STATUSES.has(task.status);
  return CLOSED_STATUSES.has(task.status);
}

export function taskMatches(task: Task, filters: TaskFilters, state: DashboardState): boolean {
  if (!matchesPrimary(task, filters.primary)) return false;
  if (filters.status !== "all" && task.status !== filters.status) return false;
  if (filters.priority !== "all" && task.priority !== filters.priority) return false;
  if (filters.environment !== "all" && task.runtime_environment !== filters.environment) return false;
  if (filters.project !== "all" && !taskProjects(task, state).includes(filters.project)) return false;
  if (filters.capability !== "all" && task.capability_level !== filters.capability) return false;
  const query = filters.query.trim().toLocaleLowerCase("zh-CN");
  if (!query) return true;
  return [task.id, task.title, task.description, ...taskProjects(task, state)]
    .filter(Boolean)
    .some((value) => String(value).toLocaleLowerCase("zh-CN").includes(query));
}

export type HeaderFilterKey = "status" | "priority" | "environment" | "project" | "capability";

export function contextualFilterValues(
  key: HeaderFilterKey,
  tasks: Task[],
  filters: TaskFilters,
  state: DashboardState,
): string[] {
  const contextualFilters = { ...filters, [key]: "all" };
  const candidates = tasks.filter((task) => taskMatches(task, contextualFilters, state));
  const values = candidates.flatMap((task) => {
    if (key === "project") return taskProjects(task, state);
    if (key === "capability") return task.capability_level ? [task.capability_level] : [];
    return [String(task[key === "status" ? "status" : key === "priority" ? "priority" : "runtime_environment"] ?? "")];
  });
  return [...new Set(values.filter(Boolean))].sort((left, right) => left.localeCompare(right, "zh-CN"));
}

export function resetInvalidFilters(filters: TaskFilters, tasks: Task[], state: DashboardState): TaskFilters {
  let next = filters;
  for (const key of ["status", "priority", "environment", "project", "capability"] as HeaderFilterKey[]) {
    const selected = next[key];
    if (selected !== "all" && !contextualFilterValues(key, tasks, next, state).includes(selected)) {
      next = { ...next, [key]: "all" };
    }
  }
  return next;
}

export function executionConfig(task: Task, state: DashboardState) {
  if (!task.capability_level) return null;
  const profile = state.runtime_config.execution_profiles?.[task.runtime_environment];
  return task.runtime_environment === "self_hosted_agent"
    ? profile?.providers?.[task.provider_id ?? ""]?.capabilities?.[task.capability_level] ?? null
    : profile?.capabilities?.[task.capability_level] ?? null;
}

export function executionConfigLabel(task: Task, state: DashboardState): string {
  const config = executionConfig(task, state);
  if (!config?.model) return "配置不可用";
  return `${config.model} · ${config.reasoning ?? "unknown"} · ${config.attempt_timeout_seconds ?? "--"}s · 重试 ${config.max_retries ?? "--"}`;
}

export type DependencyIndicator = { color: "red" | "yellow" | "green"; label: string };

export function dependencyIndicatorState(task: Task, tasks: Task[]): DependencyIndicator | null {
  const dependencyIds = (task.depends_on ?? []).filter(Boolean);
  if (!dependencyIds.length) return null;
  const statuses = dependencyIds.map((id) => tasks.find((candidate) => candidate.id === id)?.status);
  if (statuses.some((status) => !status || ["DRAFT", "PENDING", "FAILED", "CANCELLED"].includes(status))) {
    return { color: "red", label: "依赖状态：存在未执行、失败、取消、缺失或异常依赖" };
  }
  if (statuses.some((status) => status !== undefined && !["SUCCEEDED", "CONFIRMED"].includes(status))) {
    return { color: "yellow", label: "依赖状态：存在执行中或等待中的依赖" };
  }
  return { color: "green", label: "依赖状态：全部依赖已成功完成" };
}

export interface ScopeBlockGroup {
  blockerId: string;
  blockerTitle: string;
  scopes: Array<{ scopeKey: string; scope: string }>;
}

export function scopeBlockGroups(task: Task, tasks: Task[]): ScopeBlockGroup[] {
  if (task.status !== "PENDING" || !task.blocked_by_task_ids.length) return [];
  const tasksById = new Map(tasks.map((candidate) => [candidate.id, candidate]));
  const scopeByKey = new Map(task.scope_keys.map((key, index) => [key, task.scope[index] ?? key]));
  return task.blocked_by_task_ids.map((blockerId) => {
    const detailedKeys = task.blocking_scopes
      .filter((item) => item.blocker_task_id === blockerId)
      .map((item) => item.requested_scope_key)
      .filter(Boolean);
    const scopeKeys = [...new Set(detailedKeys.length ? detailedKeys : task.blocked_scope_keys)];
    return {
      blockerId,
      blockerTitle: tasksById.get(blockerId)?.title?.trim() ?? "",
      scopes: scopeKeys.map((scopeKey) => ({ scopeKey, scope: scopeByKey.get(scopeKey) ?? scopeKey })),
    };
  });
}

export function isHeartbeatLate(task: Task, state: DashboardState): boolean {
  if (!ACTIVE_STATUSES.has(task.status) || !task.heartbeat_at) return false;
  const timestamp = new Date(task.heartbeat_at).getTime();
  return Number.isFinite(timestamp) && Date.now() - timestamp > (state.settings?.stalled_after_seconds ?? 300) * 1000;
}

export function primaryCounts(tasks: Task[]): Record<PrimaryFilter, number> {
  return {
    all: tasks.length,
    draft: tasks.filter((task) => !task.archived_at && task.status === "DRAFT").length,
    review: tasks.filter((task) => !task.archived_at && REVIEW_STATUSES.has(task.status)).length,
    pending: tasks.filter((task) => !task.archived_at && task.status === "PENDING").length,
    queued: tasks.filter((task) => !task.archived_at && task.status === "QUEUED").length,
    active: tasks.filter((task) => !task.archived_at && ACTIVE_STATUSES.has(task.status)).length,
    closed: tasks.filter((task) => !task.archived_at && CLOSED_STATUSES.has(task.status)).length,
    archived: tasks.filter((task) => Boolean(task.archived_at)).length,
  };
}

export function environmentName(environment: string, state: DashboardState): string {
  return state.runtime_config.runtime_environments[environment]?.name ?? environment;
}
