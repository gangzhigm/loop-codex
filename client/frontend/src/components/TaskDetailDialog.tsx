import { X } from "lucide-react";
import { DashboardState, SplitSuggestion, Task } from "../types";
import {
  PREFLIGHT_LABELS, PRIORITY_LABELS, STATUS_LABELS, environmentName,
  executionConfigLabel, formatDate, formatDuration, scopeBlockGroups, taskProjects,
} from "../utils";

function textValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "";
  return JSON.stringify(value);
}

function ListBlock({ title, values, empty = "无", code = false }: { title: string; values?: unknown[]; empty?: string; code?: boolean }) {
  const normalized = (values ?? []).map(textValue).filter(Boolean);
  return <section className="detail-block"><h3>{title}</h3>{normalized.length ? <ul className={code ? "scope-list" : undefined}>{normalized.map((value, index) => <li key={`${value}-${index}`}>{code ? <code>{value}</code> : value}</li>)}</ul> : <p className="muted">{empty}</p>}</section>;
}

function PlannerSplitSuggestions({ suggestions }: { suggestions?: SplitSuggestion[] }) {
  if (!suggestions?.length) return null;
  return <section className="detail-block"><h3>Planner 拆分记录</h3>
    {suggestions.map((group, index) => <div className="split-suggestion" key={`${group.reason}-${index}`}><strong>{group.reason}</strong>
      {group.tasks.map((proposed) => <div className="split-task" key={proposed.id}>
        <strong>{proposed.id} · {proposed.title} · {proposed.capability_level}</strong>
        <span>{proposed.description}</span>
        <span>验收：{proposed.acceptance.join("；")}</span>
        <ul className="scope-list">{proposed.scope.map((scope) => <li key={scope}><code>{scope}</code></li>)}</ul>
        <span>依赖：{proposed.depends_on.join("、") || "无"}；可并行：{proposed.parallel_with.join("、") || "无"}</span>
      </div>)}
    </div>)}
  </section>;
}

function Attachments({ task }: { task: Task }) {
  const attachments = task.attachments ?? [];
  const images = attachments.filter((item) => /\.(?:avif|gif|jpe?g|png|webp)$/i.test(item.path));
  const files = attachments.filter((item) => !images.includes(item));
  return <section className="detail-block"><h3>附件</h3>{attachments.length ? <>
    {images.length > 0 && <div className="attachment-grid">{images.map((item) => {
      const query = new URLSearchParams({ task_id: task.id, path: item.path });
      const url = `/api/attachment?${query}`;
      return <figure key={item.path}><a href={url} target="_blank" rel="noopener" aria-label={`查看附件原图：${item.path}`}><img src={url} alt={`${task.title} · ${item.role || "source"}`} loading="lazy" /></a><figcaption><strong>{item.role || "source"}</strong><span>{item.path}</span></figcaption></figure>;
    })}</div>}
    {files.length > 0 && <ul>{files.map((item) => <li key={item.path}>{item.role || "source"}: {item.path}</li>)}</ul>}
  </> : <p className="muted">无</p>}</section>;
}

function ResultDiagnostic({ diagnostic }: { diagnostic?: Record<string, unknown> | null }) {
  if (!diagnostic) return null;
  const values: Array<[string, unknown]> = [
    ["类别", diagnostic.category], ["HTTP", diagnostic.http_status],
    ["可重试", diagnostic.retryable === undefined ? undefined : diagnostic.retryable ? "是" : "否"],
    ["重试已耗尽", diagnostic.retry_exhausted === undefined ? undefined : diagnostic.retry_exhausted ? "是" : "否"],
    ["结束原因", diagnostic.finish_reason], ["Agent 尝试", diagnostic.agent_attempt], ["模型步骤", diagnostic.model_step],
  ];
  const shape = diagnostic.final_shape && typeof diagnostic.final_shape === "object" ? diagnostic.final_shape as Record<string, unknown> : null;
  if (shape) {
    values.push(["JSON 状态", shape.json_parse_state], ["顶层类型", shape.top_level_type], ["内容长度", shape.content_length], ["未知字段数", shape.unknown_field_count]);
    const allowed = shape.allowed_fields && typeof shape.allowed_fields === "object" ? shape.allowed_fields as Record<string, unknown> : {};
    const present = Object.entries(allowed).flatMap(([name, metadata]) => {
      const item = metadata && typeof metadata === "object" ? metadata as Record<string, unknown> : null;
      return item?.present ? [`${name}: ${String(item.type)}`] : [];
    }).join("；");
    if (present) values.push(["返回字段类型", present]);
  }
  const visible = values.filter(([, value]) => value !== null && value !== undefined);
  return <section className="detail-block"><h3>安全诊断</h3><ul>{visible.map(([label, value]) => <li key={label}><strong>{label}</strong>：{String(value)}</li>)}</ul></section>;
}

function PendingBlockers({ task, state }: { task: Task; state: DashboardState }) {
  if (task.status !== "PENDING") return null;
  const groups = scopeBlockGroups(task, state.tasks);
  return <section className="detail-block"><h3>待执行阻塞</h3>
    <h4>依赖等待</h4>{task.depends_on?.length ? <ul>{task.depends_on.map((id) => { const dependency = state.tasks.find((item) => item.id === id); return <li key={id}><code>{id}</code> · {dependency ? STATUS_LABELS[dependency.status] : "未找到"}</li>; })}</ul> : <p className="muted">无依赖等待</p>}
    <h4>范围锁等待</h4>{groups.length ? <div className="conflict-detail-list">{groups.map((group) => <div className="scope-conflict" key={group.blockerId}><strong>{group.blockerTitle || "阻塞任务标题未找到"}</strong><code>{group.blockerId}</code><ul>{group.scopes.map((scope) => <li key={scope.scopeKey}><code>{scope.scope}</code><span>scope_key：{scope.scopeKey}</span></li>)}</ul></div>)}</div> : <p className="muted">无范围锁等待</p>}
    {task.blocked_scopes.length > 0 && <p>受影响范围：{task.blocked_scopes.map((scope) => <code key={scope}>{scope}</code>)}</p>}
    {Number.isInteger(task.scope_queue_position) && <p>当前范围队列位置：{task.scope_queue_position}</p>}
  </section>;
}

export function TaskDetailDialog({ task, state, onClose }: { task: Task | null; state: DashboardState; onClose: () => void }) {
  if (!task) return null;
  const recovery = state.recoveries.find((item) => item.task_id === task.id);
  const planner = task.planner_supplement ?? {};
  const history = [...(task.history ?? [])].reverse();
  const intervention = task.human_intervention;
  return <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="detail-dialog" role="dialog" aria-modal="true" aria-labelledby="task-dialog-title">
      <header><div><h2 id="task-dialog-title">{task.title || task.id}</h2><p>{task.id} · {STATUS_LABELS[task.status]} · revision {state.workspace.revision}</p></div><button className="icon-button" type="button" title="关闭" aria-label="关闭任务详情" onClick={onClose}><X size={18} /></button></header>
      <div className="dialog-body">
        <dl className="detail-grid">
          <div><dt>状态</dt><dd>{STATUS_LABELS[task.status]}</dd></div><div><dt>预检状态</dt><dd>{PREFLIGHT_LABELS[task.preflight_status]}</dd></div>
          <div><dt>优先级</dt><dd>{PRIORITY_LABELS[task.priority]}</dd></div><div><dt>预估能力等级</dt><dd>{task.estimated_capability_level}</dd></div>
          <div><dt>最终能力等级</dt><dd>{task.capability_level ?? "待定"}</dd></div><div><dt>运行环境</dt><dd>{environmentName(task.runtime_environment, state)} ({task.runtime_environment})</dd></div>
          <div><dt>Provider</dt><dd>{task.provider_id ?? "无"}</dd></div><div><dt>执行配置</dt><dd>{executionConfigLabel(task, state)}</dd></div>
          <div><dt>归档时间</dt><dd>{formatDate(task.archived_at)}</dd></div><div><dt>项目</dt><dd>{taskProjects(task, state).join("、") || "未识别"}</dd></div>
          <div><dt>尝试次数</dt><dd>{task.attempt ?? 0}</dd></div><div><dt>开始时间</dt><dd>{formatDate(task.started_at)}</dd></div>
          <div><dt>最后心跳</dt><dd>{formatDate(task.heartbeat_at)}</dd></div><div><dt>执行耗时</dt><dd>{task.started_at && (task.status === "RUNNING" || task.completed_at) ? formatDuration(task.started_at, task.status === "RUNNING" ? null : task.completed_at) : "--"}</dd></div>
        </dl>
        <section className="detail-block"><h3>Operator 原始定义</h3><p>{task.operator_definition.description ?? task.description ?? "无"}</p></section>
        <ListBlock title="Operator 业务验收" values={task.operator_definition.acceptance} />
        <section className="detail-block"><h3>Planner 预检</h3><dl className="planner-grid">
          <div><dt>预检状态</dt><dd>{planner.preflight_status ? PREFLIGHT_LABELS[planner.preflight_status] : "--"}</dd></div><div><dt>预估能力等级</dt><dd>{task.estimated_capability_level}</dd></div>
          <div><dt>最终能力等级</dt><dd>{planner.capability_level ?? "待定"}</dd></div><div><dt>锁模式</dt><dd>{planner.lock_mode ?? "待定"}</dd></div>
          <div><dt>预检开始</dt><dd>{formatDate(planner.started_at)}</dd></div><div><dt>预检完成</dt><dd>{formatDate(planner.completed_at)}</dd></div>
        </dl>{planner.failure && <p>原因：{planner.failure}</p>}
          <h4>精确 scope</h4>{planner.scope?.length ? <ul className="scope-list">{planner.scope.map((scope) => <li key={scope}><code>{scope}</code></li>)}</ul> : <p className="muted">尚未形成精确 scope</p>}
          <h4>技术验收补充</h4>{planner.technical_acceptance?.length ? <ul>{planner.technical_acceptance.map((item) => <li key={item}>{item}</li>)}</ul> : <p className="muted">尚无</p>}
          <h4>预检证据</h4>{planner.evidence?.length ? <ul>{planner.evidence.map((item) => <li key={item}>{item}</li>)}</ul> : <p className="muted">尚无</p>}
        </section>
        <PlannerSplitSuggestions suggestions={planner.split_suggestions} />
        <section className="detail-block"><h3>当前进展 · {task.progress.percent}%</h3><p>{task.progress.summary || "暂无进展摘要"}</p><div className="progress-track"><span style={{ width: `${task.progress.percent}%` }} /></div></section>
        {task.assigned_agent && <section className="detail-block"><h3>技术信息</h3><p>执行实例：{task.assigned_agent}</p></section>}
        <ListBlock title="已完成" values={task.progress.completed} />
        {intervention?.required && <section className="detail-block attention"><h3>人工介入</h3><p>{intervention.question ?? "需要人工确认"}</p><ListBlock title="可选方案" values={intervention.options} /></section>}
        {recovery && <section className="detail-block danger"><h3>隔离状态</h3><p>execution {recovery.execution_id} · {recovery.execution_status}<br />scope {recovery.scope_status} · 活动容量已释放<br />{recovery.quarantine_reason ?? recovery.termination_reason ?? "等待所属 Runner 确认进程结束并恢复"}</p></section>}
        {(task.result.summary || task.result.error) && <section className="detail-block"><h3>结果</h3><p>{task.result.summary ?? task.result.error}</p></section>}
        <ResultDiagnostic diagnostic={task.result.diagnostic} />
        <PendingBlockers task={task} state={state} />
        <ListBlock title="验证记录" values={task.result.verification} />
        <Attachments task={task} />
        <section className="detail-block"><h3>状态历史</h3>{history.length ? <ol className="timeline">{history.map((item, index) => <li key={index}><strong>{item.to && item.to in STATUS_LABELS ? STATUS_LABELS[item.to as keyof typeof STATUS_LABELS] : item.to ?? "状态变更"}</strong>{item.reason && <span>{item.reason}</span>}<time>{formatDate(item.at)}{item.actor ? ` · ${item.actor}` : ""}</time></li>)}</ol> : <p className="muted">无</p>}</section>
      </div>
    </section>
  </div>;
}
