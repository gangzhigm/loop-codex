import { Settings } from "lucide-react";
import { PrimaryFilter, Task } from "../types";
import { PRIMARY_FILTERS, primaryCounts } from "../utils";

interface Props {
  tasks: Task[];
  activeFilter: PrimaryFilter;
  onFilterChange: (filter: PrimaryFilter) => void;
  onOpenSettings: () => void;
}

export function SummaryHeader({ tasks, activeFilter, onFilterChange, onOpenSettings }: Props) {
  const counts = primaryCounts(tasks);
  const current = tasks.filter((task) => !task.archived_at);
  const total = Math.max(1, current.length);
  const segments = [counts.closed, counts.active, counts.review, counts.queued, counts.draft + counts.pending];

  return (
    <header className="app-header">
      <a className="brand" href="/" aria-label="Local Agent Loop">
        <span className="brand-mark" aria-hidden="true">LA</span>
        <span className="brand-name">Local Agent Loop</span>
      </a>
      <div className="header-status">
        <div className="summary-band" aria-label="任务总览">
          {PRIMARY_FILTERS.map((item) => (
            <button
              key={item.id}
              type="button"
              className="metric"
              aria-pressed={activeFilter === item.id}
              title={item.help}
              onClick={() => onFilterChange(item.id)}
            >
              <span>{item.label}</span>
              <strong>{counts[item.id]}</strong>
            </button>
          ))}
        </div>
        <div className="distribution" aria-label="当前任务分布">
          {segments.map((value, index) => <span key={index} style={{ width: `${(value / total) * 100}%` }} />)}
        </div>
      </div>
      <nav className="header-actions" aria-label="应用操作">
        <a href="/runtime-logs.html">运行日志</a>
        <a href="/operations.html">运维配置</a>
        <button className="icon-button" type="button" title="Provider 密钥" aria-label="Provider 密钥" onClick={onOpenSettings}>
          <Settings size={18} />
        </button>
      </nav>
    </header>
  );
}
