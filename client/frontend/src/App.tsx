import { AlertCircle, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { archiveTask, controlService, fetchDashboardState, ServiceControlAction, ServiceControlTarget } from "./api";
import { CapacityPanel } from "./components/CapacityPanel";
import { SecretDrawer } from "./components/SecretDrawer";
import { SummaryHeader } from "./components/SummaryHeader";
import { TaskDetailDialog } from "./components/TaskDetailDialog";
import { TaskTable } from "./components/TaskTable";
import { DashboardState, PrimaryFilter, Task, TaskFilters } from "./types";
import { resetInvalidFilters } from "./utils";

const INITIAL_FILTERS: TaskFilters = { primary: "pending", status: "all", priority: "all", environment: "all", project: "all", capability: "all", query: "" };

export default function App() {
  const [state, setState] = useState<DashboardState | null>(null);
  const [csrfToken, setCsrfToken] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [filters, setFilters] = useState<TaskFilters>(INITIAL_FILTERS);
  const [sortDirection, setSortDirection] = useState<"asc" | "desc">("desc");
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [pendingService, setPendingService] = useState<string | null>(null);
  const [archivingTaskId, setArchivingTaskId] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const result = await fetchDashboardState();
      setState(result.state);
      setCsrfToken(result.csrfToken);
      setNotice(null);
      return true;
    } catch (caught) {
      setNotice(`无法读取 /api/state，页面保留最后一次有效数据。${caught instanceof Error ? caught.message : String(caught)}`);
      return false;
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    const interval = Math.max(500, state?.runtime_config.dashboard?.poll_interval_ms ?? 2000);
    const timer = window.setInterval(() => void refresh(), interval);
    return () => window.clearInterval(timer);
  }, [refresh, state?.runtime_config.dashboard?.poll_interval_ms]);
  useEffect(() => {
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") { setSelectedTaskId(null); setSettingsOpen(false); } };
    document.addEventListener("keydown", close);
    return () => document.removeEventListener("keydown", close);
  }, []);
  useEffect(() => {
    if (!state) return;
    setFilters((current) => resetInvalidFilters(current, state.tasks, state));
  }, [state]);

  const selectedTask = useMemo(() => state?.tasks.find((task) => task.id === selectedTaskId) ?? null, [state, selectedTaskId]);

  const setPrimaryFilter = (primary: PrimaryFilter) => setFilters({ ...INITIAL_FILTERS, primary });
  const handleServiceControl = async (service: ServiceControlTarget, action: ServiceControlAction) => {
    if (!csrfToken || pendingService) return;
    const labels: Record<ServiceControlAction, string> = { start: "启动", stop: "停止", restart: "重启", enable: "开启自动化", disable: "关闭自动化", trigger: "单次触发" };
    const names: Record<ServiceControlTarget, string> = { supervisor: "Supervisor", scheduler: "Scheduler", runner: "Runner", planner: "Planner", dispatcher: "Dispatcher" };
    if (!window.confirm(`${labels[action]} ${names[service]}？`)) return;
    setPendingService(`${service}:${action}`);
    try { await controlService(service, action, csrfToken); await refresh(); }
    catch (caught) { setNotice(`服务操作失败。${caught instanceof Error ? caught.message : String(caught)}`); }
    finally { setPendingService(null); }
  };
  const handleArchive = async (task: Task) => {
    if (archivingTaskId) return;
    const message = task.status === "SUCCEEDED" ? `任务 ${task.id} 尚未人工确认。继续将先确认，再归档。` : `确认归档任务 ${task.id}？`;
    if (!window.confirm(message)) return;
    setArchivingTaskId(task.id);
    try { await archiveTask(task.id, task.row_version); await refresh(); }
    catch (caught) { setNotice(`归档失败：${caught instanceof Error ? caught.message : String(caught)}`); }
    finally { setArchivingTaskId(null); }
  };

  return (
    <div className="app-shell">
      <SummaryHeader tasks={state?.tasks ?? []} activeFilter={filters.primary} onFilterChange={setPrimaryFilter} onOpenSettings={() => setSettingsOpen(true)} />
      <main>
        {notice && <div className="notice" role="alert"><AlertCircle size={17} /><span>{notice}</span><button className="icon-button small" type="button" title="关闭提示" aria-label="关闭提示" onClick={() => setNotice(null)}><X size={15} /></button></div>}
        {state ? <>
          <div className="content-grid">
            <TaskTable state={state} filters={filters} sortDirection={sortDirection} archivingTaskId={archivingTaskId} onFiltersChange={setFilters} onSortChange={() => setSortDirection(sortDirection === "asc" ? "desc" : "asc")} onOpenTask={(task) => setSelectedTaskId(task.id)} onArchive={(task) => void handleArchive(task)} />
            <CapacityPanel state={state} pendingService={pendingService} onControl={(service, action) => void handleServiceControl(service, action)} />
          </div>
        </> : <div className="loading-state"><span className="loading-bar" /><strong>正在载入任务状态</strong><span>等待本机 Dashboard 服务响应</span></div>}
      </main>
      {state && <TaskDetailDialog task={selectedTask} state={state} onClose={() => setSelectedTaskId(null)} />}
      <SecretDrawer open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </div>
  );
}
