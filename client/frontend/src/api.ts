import { DashboardState, SecretProvider, TASK_SCHEMA_VERSION } from "./types";

const STATE_ENDPOINT = "/api/state";
const TASK_ACTION_ENDPOINT = "/api/task-action";
const SERVICE_ACTION_ENDPOINT = "/api/service-action";
const SECRET_API_ENDPOINT = "/api/secrets";
const VALID_STATUSES = new Set(["DRAFT", "NEEDS_REVIEW", "PENDING", "QUEUED", "CLAIMED", "RUNNING", "WAITING_CONFLICT", "WAITING_HUMAN", "BLOCKED", "STALLED", "SUCCEEDED", "CONFIRMED", "FAILED", "CANCELLED"]);
const VALID_PRIORITIES = new Set(["blocker", "critical", "high", "medium", "low"]);
const VALID_CAPABILITIES = new Set(["L1", "L2", "L3", "L4", "L5"]);
const VALID_PREFLIGHT_STATUSES = new Set(["UNINSPECTED", "QUEUED", "INSPECTING", "READY", "FAILED"]);

function executionConfig(state: Partial<DashboardState>, environment: string, providerId: string | null, capability: string): unknown {
  const profile = state.runtime_config?.execution_profiles?.[environment];
  return environment === "self_hosted_agent"
    ? profile?.providers?.[providerId ?? ""]?.capabilities?.[capability as "L1"]
    : profile?.capabilities?.[capability as "L1"];
}

async function jsonResponse(response: Response): Promise<Record<string, unknown>> {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(typeof payload.error === "string" ? payload.error : `HTTP ${response.status}`);
  }
  return payload;
}

export function validateDashboardState(value: unknown): DashboardState {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("状态根节点必须是对象");
  }
  const candidate = value as Partial<DashboardState>;
  const errors: string[] = [];
  if (candidate.schema_version !== TASK_SCHEMA_VERSION) errors.push(`schema_version 必须为 ${TASK_SCHEMA_VERSION}`);
  if (!candidate.workspace || !Number.isInteger(candidate.workspace.revision)) errors.push("workspace.revision 无效");
  for (const key of ["tasks", "agents", "recoveries", "services"] as const) {
    if (!Array.isArray(candidate[key])) errors.push(`${key} 必须是数组`);
  }
  if (!candidate.service_control || typeof candidate.service_control.supervisor !== "boolean" || typeof candidate.service_control.scheduler !== "boolean" || typeof candidate.service_control.runner !== "boolean") {
    errors.push("service_control 无效");
  }
  if (!candidate.scheduler_control || typeof candidate.scheduler_control.planner_automation !== "boolean" || typeof candidate.scheduler_control.dispatcher_automation !== "boolean") {
    errors.push("scheduler_control 无效");
  }
  if (!candidate.database || typeof candidate.database.status !== "string" || !candidate.database.status) {
    errors.push("database 服务状态无效");
  }
  if (!candidate.runtime_config?.runtime_environments || typeof candidate.runtime_config.runtime_environments !== "object") {
    errors.push("runtime_config.runtime_environments 无效");
  }
  const runtimeEnvironments = new Set(Object.keys(candidate.runtime_config?.runtime_environments ?? {}));
  const ids = new Set<string>();
  for (const [index, task] of (candidate.tasks ?? []).entries()) {
    if (!task || typeof task.id !== "string" || !task.id || ids.has(task.id)) errors.push(`tasks[${index}].id 缺失或重复`);
    else ids.add(task.id);
    if (!Number.isInteger(task?.row_version) || task.row_version < 1) errors.push(`任务 ${task?.id ?? index} row_version 无效`);
    if (!VALID_STATUSES.has(task?.status)) errors.push(`任务 ${task?.id ?? index} 状态无效`);
    if (!VALID_PRIORITIES.has(task?.priority)) errors.push(`任务 ${task?.id ?? index} 优先级无效`);
    if (!VALID_PREFLIGHT_STATUSES.has(task?.preflight_status)) errors.push(`任务 ${task?.id ?? index} preflight_status 无效`);
    if (!VALID_CAPABILITIES.has(task?.estimated_capability_level)) errors.push(`任务 ${task?.id ?? index} 预估能力等级无效`);
    if (task?.capability_level !== null && !VALID_CAPABILITIES.has(task?.capability_level)) errors.push(`任务 ${task?.id ?? index} 能力等级无效`);
    if (!task?.runtime_environment || !runtimeEnvironments.has(task.runtime_environment)) errors.push(`任务 ${task?.id ?? index} 运行环境缺失或未知`);
    if (task?.runtime_environment === "self_hosted_agent" && (!task.provider_id || (task.capability_level && !executionConfig(candidate, task.runtime_environment, task.provider_id, task.capability_level)))) errors.push(`任务 ${task?.id ?? index} Provider 或执行配置无效`);
    if (task?.runtime_environment !== "self_hosted_agent" && task?.provider_id !== null) errors.push(`任务 ${task?.id ?? index} Provider 路由无效`);
    if (!task?.operator_definition || !task?.planner_supplement || !task?.result) errors.push(`任务 ${task?.id ?? index} 契约字段缺失`);
    if (!Number.isInteger(task?.progress?.percent) || task.progress.percent < 0 || task.progress.percent > 100) errors.push(`任务 ${task?.id ?? index} 进度无效`);
    if (task) {
      task.blocked_by_task_ids ??= [];
      task.blocked_scopes ??= [];
      task.blocked_scope_keys ??= [];
      task.blocking_scopes ??= [];
      task.scope_queue_position ??= null;
      task.scope ??= [];
      task.scope_keys ??= [];
      if (![task.scope, task.scope_keys, task.blocked_by_task_ids, task.blocked_scopes, task.blocked_scope_keys, task.blocking_scopes].every(Array.isArray)) errors.push(`任务 ${task.id} scope 或阻塞字段无效`);
    }
  }
  for (const [index, agent] of (candidate.agents ?? []).entries()) {
    if (!runtimeEnvironments.has(agent.runtime_environment) || !VALID_CAPABILITIES.has(agent.capability_level)) errors.push(`活动 execution ${agent.id ?? index} 路由无效`);
    if (agent.runtime_environment === "self_hosted_agent" && (!agent.provider_id || !executionConfig(candidate, agent.runtime_environment, agent.provider_id, agent.capability_level))) errors.push(`活动 execution ${agent.id ?? index} Provider 配置无效`);
  }
  for (const [index, recovery] of (candidate.recoveries ?? []).entries()) {
    if (!recovery.execution_id || !ids.has(recovery.task_id) || !["STALLED", "TIMED_OUT"].includes(recovery.execution_status) || recovery.scope_status !== "QUARANTINED" || !Array.isArray(recovery.scope_keys) || !recovery.scope_keys.length) errors.push(`恢复记录 ${index} 无效`);
  }
  if (errors.length) throw new Error(errors.join("；"));
  return candidate as DashboardState;
}

export async function fetchDashboardState(): Promise<{ state: DashboardState; csrfToken: string | null }> {
  const response = await fetch(STATE_ENDPOINT, { cache: "no-store", credentials: "same-origin" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return {
    state: validateDashboardState(await response.json()),
    csrfToken: response.headers.get("X-CSRF-Token"),
  };
}

export type ServiceControlTarget = "supervisor" | "scheduler" | "runner" | "planner" | "dispatcher";
export type ServiceControlAction = "start" | "stop" | "restart" | "enable" | "disable" | "trigger";

const SERVICE_CONFIRMATIONS: Record<ServiceControlTarget, Partial<Record<ServiceControlAction, string>>> = {
  supervisor: { start: "START", stop: "STOP", restart: "RESTART" },
  scheduler: { start: "START", stop: "STOP", restart: "RESTART" },
  runner: { start: "START", stop: "STOP", restart: "RESTART" },
  planner: { enable: "ENABLE_AUTOMATION", disable: "DISABLE_AUTOMATION", trigger: "TRIGGER_ONCE" },
  dispatcher: { enable: "ENABLE_AUTOMATION", disable: "DISABLE_AUTOMATION", trigger: "TRIGGER_ONCE" },
};

export async function controlService(
  service: ServiceControlTarget,
  action: ServiceControlAction,
  csrfToken: string,
): Promise<Record<string, unknown>> {
  if (!crypto.randomUUID) throw new Error("当前浏览器不支持安全请求 ID");
  const confirmation = SERVICE_CONFIRMATIONS[service][action];
  if (!confirmation) throw new Error("不支持的服务操作");
  const response = await fetch(SERVICE_ACTION_ENDPOINT, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: JSON.stringify({ service, action, request_id: crypto.randomUUID(), confirmation }),
  });
  return jsonResponse(response);
}

export async function archiveTask(taskId: string, rowVersion: number): Promise<void> {
  const response = await fetch(TASK_ACTION_ENDPOINT, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_id: taskId, action: "archive", row_version: rowVersion }),
  });
  await jsonResponse(response);
}

export async function fetchSecretProviders(): Promise<{ providers: SecretProvider[]; csrfToken: string }> {
  const response = await fetch(SECRET_API_ENDPOINT, {
    cache: "no-store",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  });
  const payload = await jsonResponse(response);
  const csrfToken = response.headers.get("X-CSRF-Token");
  if (!Array.isArray(payload.providers) || !csrfToken) throw new Error("SecretStore 响应不完整");
  return { providers: payload.providers as SecretProvider[], csrfToken };
}

export async function mutateSecret(
  providerId: string,
  action: "set" | "rotate" | "verify" | "delete",
  csrfToken: string,
  values: Record<string, unknown> = {},
): Promise<void> {
  if (!crypto.randomUUID) throw new Error("当前浏览器不支持安全请求 ID");
  const body: Record<string, unknown> = { provider_id: providerId, action, request_id: crypto.randomUUID(), ...values };
  let requestBody = JSON.stringify(body);
  if ("secret" in body) body.secret = "";
  try {
    const response = await fetch(SECRET_API_ENDPOINT, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: requestBody,
    });
    requestBody = "";
    await jsonResponse(response);
  } finally {
    requestBody = "";
  }
}
