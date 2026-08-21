import { Archive, ArrowDown, ArrowUp, Check, Clipboard, Search, X } from "lucide-react";
import { useState } from "react";
import { DashboardState, Task, TaskFilters } from "../types";
import {
  CAPABILITY_LEVELS, PREFLIGHT_LABELS, PRIORITY_LABELS, PRIORITY_ORDER, STATUS_LABELS,
  contextualFilterValues, dependencyIndicatorState, environmentName, executionConfigLabel,
  formatDate, formatDuration, isHeartbeatLate, scopeBlockGroups, taskMatches, taskProjects,
} from "../utils";

interface Props {
  state: DashboardState;
  filters: TaskFilters;
  sortDirection: "asc" | "desc";
  archivingTaskId: string | null;
  onFiltersChange: (filters: TaskFilters) => void;
  onSortChange: () => void;
  onOpenTask: (task: Task) => void;
  onArchive: (task: Task) => void;
}

function FilterSelect({ label, value, values, labels, onChange }: { label: string; value: string; values: string[]; labels?: Record<string, string>; onChange: (value: string) => void }) {
  return <label className="filter-select"><span>{label}</span><select value={value} onChange={(event) => onChange(event.target.value)}><option value="all">全部</option>{values.map((item) => <option key={item} value={item}>{labels?.[item] ?? item}</option>)}</select></label>;
}

function TaskTime({ task }: { task: Task }) {
  const showDuration = Boolean(task.started_at && (task.status === "RUNNING" || task.completed_at));
  return <dl className="task-time-info">
    <div><dt>日期</dt><dd>{task.started_at ? formatDate(task.started_at).slice(0, 10) : "--"}</dd></div>
    <div><dt>开始</dt><dd>{task.started_at ? formatDate(task.started_at, false).slice(0, 5) : "--"}</dd></div>
    <div><dt>完成</dt><dd>{task.completed_at ? formatDate(task.completed_at, false).slice(0, 5) : "--"}</dd></div>
    <div><dt>耗时</dt><dd>{showDuration ? formatDuration(task.started_at, task.status === "RUNNING" ? null : task.completed_at) : "--"}</dd></div>
  </dl>;
}

function ScopeBlockIndicator({ task, tasks }: { task: Task; tasks: Task[] }) {
  const groups = scopeBlockGroups(task, tasks);
  if (!groups.length) return null;
  const firstLabel = groups[0].blockerTitle || groups[0].blockerId || "未知阻塞任务";
  return <details className="scope-wait" onClick={(event) => event.stopPropagation()}>
    <summary title="范围锁等待">范围等待：{firstLabel}{groups.length > 1 ? ` +${groups.length - 1}` : ""}{Number.isInteger(task.scope_queue_position) ? ` · 队列 ${task.scope_queue_position}` : ""}</summary>
    <div className="scope-popover" aria-label="阻塞任务详情">
      {groups.map((group) => <div className="scope-conflict" key={group.blockerId}>
        <strong>{group.blockerTitle || "阻塞任务标题未找到"}</strong><code>{group.blockerId}</code>
        <ul>{group.scopes.map((scope) => <li key={scope.scopeKey}><code>{scope.scope}</code><span>scope_key：{scope.scopeKey}</span></li>)}</ul>
      </div>)}
    </div>
  </details>;
}

export function TaskTable({ state, filters, sortDirection, archivingTaskId, onFiltersChange, onSortChange, onOpenTask, onArchive }: Props) {
  const [copyFeedback, setCopyFeedback] = useState<{ taskId: string; ok: boolean } | null>(null);
  const visible = state.tasks.filter((task) => taskMatches(task, filters, state)).sort((left, right) => {
    const leftTime = new Date(left.started_at ?? "").getTime();
    const rightTime = new Date(right.started_at ?? "").getTime();
    const leftValid = Number.isFinite(leftTime);
    const rightValid = Number.isFinite(rightTime);
    if (leftValid !== rightValid) return leftValid ? -1 : 1;
    if (leftValid && rightValid && leftTime !== rightTime) return sortDirection === "asc" ? leftTime - rightTime : rightTime - leftTime;
    const priorityDifference = PRIORITY_ORDER[left.priority] - PRIORITY_ORDER[right.priority];
    return priorityDifference || (new Date(right.updated_at ?? "").getTime() || 0) - (new Date(left.updated_at ?? "").getTime() || 0);
  });
  const statuses = contextualFilterValues("status", state.tasks, filters, state);
  const priorities = contextualFilterValues("priority", state.tasks, filters, state);
  const capabilities = contextualFilterValues("capability", state.tasks, filters, state).filter((value) => CAPABILITY_LEVELS.includes(value as (typeof CAPABILITY_LEVELS)[number]));
  const environments = contextualFilterValues("environment", state.tasks, filters, state);
  const projects = contextualFilterValues("project", state.tasks, filters, state);
  const canArchive = filters.primary === "closed";
  const patchFilter = (key: keyof TaskFilters, value: string) => onFiltersChange({ ...filters, [key]: value });

  const copyTaskId = async (taskId: string) => {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(taskId);
      setCopyFeedback({ taskId, ok: true });
    } catch {
      setCopyFeedback({ taskId, ok: false });
    }
    window.setTimeout(() => setCopyFeedback((current) => current?.taskId === taskId ? null : current), 1800);
  };

  return <section className="task-section" aria-labelledby="tasks-heading">
    <div className="task-toolbar">
      <div><h2 id="tasks-heading">任务</h2><span>{filters.primary === "all" ? "全部" : filters.primary === "archived" ? "已归档" : "当前"} {visible.length} 项</span></div>
      <label className="search-field"><Search size={16} /><input type="search" value={filters.query} placeholder="搜索 ID、标题或项目" aria-label="搜索任务" onChange={(event) => patchFilter("query", event.target.value)} /></label>
    </div>
    <div className="filter-bar" aria-label="任务筛选">
      <FilterSelect label="状态" value={filters.status} values={statuses} labels={STATUS_LABELS} onChange={(value) => patchFilter("status", value)} />
      <FilterSelect label="优先级" value={filters.priority} values={priorities} labels={PRIORITY_LABELS} onChange={(value) => patchFilter("priority", value)} />
      <FilterSelect label="能力" value={filters.capability} values={capabilities} onChange={(value) => patchFilter("capability", value)} />
      <FilterSelect label="环境" value={filters.environment} values={environments} labels={Object.fromEntries(environments.map((item) => [item, environmentName(item, state)]))} onChange={(value) => patchFilter("environment", value)} />
      <FilterSelect label="项目" value={filters.project} values={projects} onChange={(value) => patchFilter("project", value)} />
      <button type="button" className="sort-button" onClick={onSortChange} title={`按日期${sortDirection === "asc" ? "正序" : "倒序"}`} aria-label={`按日期${sortDirection === "asc" ? "正序" : "倒序"}`}>{sortDirection === "asc" ? <ArrowUp size={16} /> : <ArrowDown size={16} />}日期</button>
    </div>
    {visible.length ? <div className="table-wrap"><table>
      <thead><tr><th>任务</th><th>运行状态</th><th>主/子状态</th><th>优先级</th><th>能力</th><th>环境</th><th>项目</th><th>时间</th><th><span className="sr-only">操作</span></th></tr></thead>
      <tbody>{visible.map((task) => {
        const dependency = dependencyIndicatorState(task, state.tasks);
        const recovery = state.recoveries.find((item) => item.task_id === task.id);
        const copied = copyFeedback?.taskId === task.id;
        return <tr key={task.id} tabIndex={0} onClick={() => onOpenTask(task)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onOpenTask(task); } }}>
          <td data-label="任务"><div className="task-title"><strong>{task.title || task.id}</strong><span>{task.id}</span></div><button className={`copy-button${copied ? " has-feedback" : ""}`} type="button" title="复制任务 ID" aria-label={`复制任务 ID ${task.id}`} onClick={(event) => { event.stopPropagation(); void copyTaskId(task.id); }}>{copied ? (copyFeedback.ok ? <Check size={13} /> : <X size={13} />) : <Clipboard size={13} />}<span className="sr-only" aria-live="polite">{copied ? (copyFeedback.ok ? "已复制" : "复制失败") : ""}</span></button></td>
          <td data-label="运行状态"><div className="status-stack"><div className="status-line"><span className={`status-badge status-${task.status.toLowerCase().replaceAll("_", "-")}`}>{STATUS_LABELS[task.status]}{isHeartbeatLate(task, state) ? " · 心跳超时" : ""}</span>{dependency && <span className={`dependency-indicator is-${dependency.color}`} role="img" aria-label={dependency.label} title={dependency.label} />}</div>{recovery && <small className="recovery-note">{recovery.execution_status} · scope 隔离</small>}{task.archived_at && <small className="archive-note">归档于 {formatDate(task.archived_at, false)}</small>}<ScopeBlockIndicator task={task} tasks={state.tasks} /></div></td>
          <td data-label="主/子状态"><div className="phase-stack"><span><small>主</small><strong>{STATUS_LABELS[task.status]}</strong></span><span><small>子</small><strong>{PREFLIGHT_LABELS[task.preflight_status]}</strong></span></div></td>
          <td data-label="优先级"><span className={`priority priority-${task.priority}`}>{PRIORITY_LABELS[task.priority]}</span></td>
          <td data-label="能力">{task.capability_level ? <span className={`capability-badge capability-${task.capability_level.toLowerCase()}`} title={executionConfigLabel(task, state)}>{task.capability_level}</span> : <span className="muted">待预检</span>}</td>
          <td data-label="环境"><span className="environment-badge" title={`${task.runtime_environment}${task.provider_id ? ` · ${task.provider_id}` : ""}`}>{environmentName(task.runtime_environment, state)}{task.provider_id ? ` · ${task.provider_id}` : ""}</span></td>
          <td data-label="项目"><div className="project-list">{taskProjects(task, state).map((project) => <span key={project}>{project}</span>)}{!taskProjects(task, state).length && <span className="muted">未识别</span>}</div></td>
          <td data-label="时间"><TaskTime task={task} /></td>
          <td className="row-action">{canArchive && <button type="button" className="icon-button small" disabled={archivingTaskId === task.id} title="归档任务" aria-label={`归档任务 ${task.id}`} onClick={(event) => { event.stopPropagation(); onArchive(task); }}><Archive size={15} /></button>}</td>
        </tr>;
      })}</tbody>
    </table></div> : <div className="empty-state"><strong>{state.tasks.length ? "没有匹配任务" : "当前没有任务"}</strong><span>{state.tasks.length ? "调整筛选条件或搜索内容" : "任务数据源已连接"}</span></div>}
  </section>;
}
