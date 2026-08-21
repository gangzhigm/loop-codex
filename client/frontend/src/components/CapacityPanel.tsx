import { Bot, ChevronDown, Database, Gauge, Play, RefreshCw, Server, Square } from "lucide-react";
import type { ReactNode } from "react";
import type { ServiceControlAction, ServiceControlTarget } from "../api";
import { DashboardState } from "../types";
import { CAPABILITY_LEVELS, environmentName, heartbeatAge, heartbeatTimestamp, runtimeTone } from "../utils";

const STATUS_LABELS: Record<string, string> = {
  HEALTHY: "正常", RESTARTED: "已恢复", RESTARTING: "恢复中", DISABLED: "已关闭",
  STOPPING: "停止中", STOP_FAILED: "停止失败", BLOCKED: "恢复受阻", UNHEALTHY: "异常",
  NEEDS_ATTENTION: "需要处理", STALE: "心跳超时", PROCESS_MISSING: "进程不存在",
  IDENTITY_MISMATCH: "身份不匹配", WORKER_MISSING: "AI 进程不存在", INVALID_STATE: "状态无效",
  UNAVAILABLE: "不可观察", IDLE: "空闲", OBSERVING: "观察队列", UNKNOWN: "无快照",
};

interface Props {
  state: DashboardState;
  pendingService: string | null;
  onControl: (service: ServiceControlTarget, action: ServiceControlAction) => void;
}

function ServiceActions({
  service,
  enabled,
  pending,
  onControl,
}: {
  service: "supervisor" | "scheduler" | "runner";
  enabled: boolean;
  pending: string | null;
  onControl: Props["onControl"];
}) {
  const action = enabled ? "stop" : "start";
  const label = `${enabled ? "停止" : "启动"} ${service}`;
  return <span className="service-monitor-actions" aria-label={`${service} 服务控制`}>
    <button type="button" className="icon-button small" disabled={pending !== null} title={label} aria-label={label} onClick={() => onControl(service, action)}>
      {enabled ? <Square size={13} /> : <Play size={14} />}
    </button>
    <button type="button" className="icon-button small" disabled={!enabled || pending !== null} title={`重启 ${service}`} aria-label={`重启 ${service}`} onClick={() => onControl(service, "restart")}><RefreshCw size={14} /></button>
  </span>;
}

function ChainActions({
  service,
  automationEnabled,
  pending,
  onControl,
}: {
  service: "planner" | "dispatcher";
  automationEnabled: boolean;
  pending: string | null;
  onControl: Props["onControl"];
}) {
  const name = service === "planner" ? "Planner" : "Dispatcher";
  const action = automationEnabled ? "disable" : "enable";
  const label = automationEnabled ? "关闭自动化" : "开启自动化";
  return <span className="service-monitor-actions" aria-label={`${name} 调度控制`}>
    <button type="button" className="automation-button" disabled={pending !== null} title={`${label} ${name}`} aria-label={`${label} ${name}`} onClick={() => onControl(service, action)}>
      {automationEnabled ? <Square size={12} /> : <Play size={12} />}
    </button>
    <button type="button" className="icon-button small" disabled={pending !== null} title={`单次触发 ${name}`} aria-label={`单次触发 ${name}`} onClick={() => onControl(service, "trigger")}><Play size={14} /></button>
  </span>;
}

function ServiceRow({
  name,
  icon,
  status,
  checkedAt,
  pid,
  message,
  actions,
}: {
  name: string;
  icon: ReactNode;
  status?: string;
  checkedAt?: string | null;
  pid?: number | null;
  message?: string;
  actions?: ReactNode;
}) {
  const actualStatus = status ?? "UNKNOWN";
  const tone = runtimeTone(actualStatus);
  return <li className="service-monitor-row" data-tone={tone}>
    <span className="service-monitor-icon" aria-hidden="true">{icon}</span>
    <span className="service-monitor-main">
      <span className="service-monitor-title"><strong>{name}</strong><span className="service-monitor-state" data-tone={tone}><i />{STATUS_LABELS[actualStatus] ?? actualStatus}</span></span>
      <span className="service-monitor-meta"><span>{pid ? `PID ${pid}` : message || "尚无状态快照"}</span><span title={checkedAt ?? undefined}>{heartbeatAge(checkedAt)}</span></span>
    </span>
    {actions}
  </li>;
}

export function CapacityPanel({ state, pendingService, onControl }: Props) {
  const maximum = state.settings?.global_max_active_executions ?? 8;
  const services = new Map(state.services.map((service) => [service.component, service]));
  const supervisor = services.get("supervisor");
  const scheduler = services.get("scheduler");
  const runner = services.get("runner");
  const database = state.database;
  return <aside className="capacity-panel" aria-label="服务监控和调度容量">
    <details open>
      <summary><span><Server size={17} />服务监控</span><strong>6 项</strong><ChevronDown size={16} /></summary>
      <ul className="service-monitor-list">
        <ServiceRow name="Supervisor" icon={<Server size={15} />} status={supervisor?.status} checkedAt={heartbeatTimestamp(supervisor)} pid={supervisor?.pid} message={supervisor?.message} actions={<ServiceActions service="supervisor" enabled={state.service_control.supervisor} pending={pendingService} onControl={onControl} />} />
        <ServiceRow name="Scheduler" icon={<Gauge size={15} />} status={scheduler?.status} checkedAt={heartbeatTimestamp(scheduler)} pid={scheduler?.pid} message={scheduler?.message} actions={<ServiceActions service="scheduler" enabled={state.service_control.scheduler} pending={pendingService} onControl={onControl} />} />
        <ServiceRow name="Runner" icon={<Bot size={15} />} status={runner?.status} checkedAt={heartbeatTimestamp(runner)} pid={runner?.pid} message={runner?.message} actions={<ServiceActions service="runner" enabled={state.service_control.runner} pending={pendingService} onControl={onControl} />} />
        <ServiceRow name="Planner" icon={<Gauge size={15} />} status={scheduler?.status} checkedAt={heartbeatTimestamp(scheduler)} pid={scheduler?.pid} message={scheduler?.message} actions={<ChainActions service="planner" automationEnabled={state.scheduler_control.planner_automation} pending={pendingService} onControl={onControl} />} />
        <ServiceRow name="Dispatcher" icon={<Gauge size={15} />} status={scheduler?.status} checkedAt={heartbeatTimestamp(scheduler)} pid={scheduler?.pid} message={scheduler?.message} actions={<ChainActions service="dispatcher" automationEnabled={state.scheduler_control.dispatcher_automation} pending={pendingService} onControl={onControl} />} />
        <ServiceRow name="数据库" icon={<Database size={15} />} status={database?.status} checkedAt={database?.checked_at} pid={null} message={database?.message ?? "等待数据库状态。"} />
      </ul>
    </details>
    <details open>
      <summary><span><Gauge size={17} />调度容量</span><strong>{state.agents.length}/{maximum}</strong><ChevronDown size={16} /></summary>
      <ul className="capacity-list">
        {CAPABILITY_LEVELS.map((level) => {
          const active = state.agents.filter((agent) => agent.capability_level === level);
          const queued = state.tasks.filter((task) => task.capability_level === level && task.status === "QUEUED").length;
          return <li key={level}>
            <span className={`capability-badge capability-${level.toLowerCase()}`}>{level}</span>
            <span><strong>{active.length} 活动</strong><small>{queued} 待执行{active[0] ? ` · ${environmentName(active[0].runtime_environment, state)}` : ""}</small></span>
          </li>;
        })}
      </ul>
    </details>
  </aside>;
}
