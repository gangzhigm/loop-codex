/**
 * Dashboard 部署校验：核对 React/TypeScript 源码、Vite 构建产物和独立运维页面。
 * 本检查只读取 UTF-8 文本，不启动服务、不访问网络，也不读取任何敏感配置。
 */

import { access, readFile } from "node:fs/promises";
import { constants } from "node:fs";
import { resolve } from "node:path";

const repositoryRoot = resolve(process.argv[2] ?? ".");
const frontendRoot = resolve(repositoryRoot, "client/frontend");
const distRoot = resolve(repositoryRoot, "client/dist");
const clientRoot = resolve(repositoryRoot, "client");

const paths = {
  package: resolve(frontendRoot, "package.json"),
  app: resolve(frontendRoot, "src/App.tsx"),
  api: resolve(frontendRoot, "src/api.ts"),
  types: resolve(frontendRoot, "src/types.ts"),
  utilities: resolve(frontendRoot, "src/utils.ts"),
  styles: resolve(frontendRoot, "src/styles.css"),
  capacity: resolve(frontendRoot, "src/components/CapacityPanel.tsx"),
  tasks: resolve(frontendRoot, "src/components/TaskTable.tsx"),
  header: resolve(frontendRoot, "src/components/SummaryHeader.tsx"),
  detail: resolve(frontendRoot, "src/components/TaskDetailDialog.tsx"),
  secrets: resolve(frontendRoot, "src/components/SecretDrawer.tsx"),
  server: resolve(clientRoot, "dashboard_server.py"),
  dist: resolve(distRoot, "index.html"),
  operationsHtml: resolve(clientRoot, "operations.html"),
  operationsJavaScript: resolve(clientRoot, "operations.js"),
  operationsCss: resolve(clientRoot, "operations.css"),
  runtimeLogsHtml: resolve(clientRoot, "runtime-logs.html"),
  runtimeLogsJavaScript: resolve(clientRoot, "runtime-logs.js"),
  runtimeLogsCss: resolve(clientRoot, "runtime-logs.css"),
};

const entries = await Promise.all(Object.entries(paths).map(async ([key, path]) => [key, await readFile(path, "utf8")]));
const source = Object.fromEntries(entries);
const errors = [];
const assert = (condition, message) => { if (!condition) errors.push(message); };

const manifest = JSON.parse(source.package);
assert(manifest.dependencies?.react, "前端未声明 React 依赖");
assert(manifest.dependencies?.["lucide-react"], "前端未声明 lucide-react 图标依赖");
assert(manifest.devDependencies?.typescript && manifest.devDependencies?.vite, "前端未声明 TypeScript/Vite 构建依赖");
assert(manifest.scripts?.build === "tsc --noEmit && vite build", "前端构建命令未执行类型检查和 Vite 构建");

assert(source.types.includes('export const TASK_SCHEMA_VERSION = "3.9.0"'), "Schema 契约不正确");
assert(source.api.includes("validateDashboardState"), "缺少状态响应运行时校验");
assert(source.api.includes('const STATE_ENDPOINT = "/api/state"'), "缺少状态 API");
assert(source.api.includes('const SERVICE_ACTION_ENDPOINT = "/api/service-action"'), "缺少服务控制 API");
assert(source.api.includes("SERVICE_CONFIRMATIONS") && source.api.includes("TRIGGER_ONCE"), "服务控制 API 缺少自动化或单次触发确认");
assert(source.api.includes('const SECRET_API_ENDPOINT = "/api/secrets"'), "缺少 SecretStore API");
assert(source.api.includes('credentials: "same-origin"'), "敏感接口请求未限定同源凭据");

for (const component of ["SummaryHeader", "TaskTable", "CapacityPanel", "TaskDetailDialog", "SecretDrawer"]) {
  assert(source.app.includes(`<${component}`), `主应用未挂载 ${component}`);
}
assert(!source.app.includes("RuntimeOverview") && !source.app.includes("运行链路"), "主应用仍挂载运行链路");
assert(source.app.includes("window.setInterval"), "Dashboard 未保持自动轮询");
assert(source.capacity.includes("服务监控") && source.capacity.includes("Supervisor") && source.capacity.includes("Scheduler") && source.capacity.includes("Planner") && source.capacity.includes("Dispatcher") && source.capacity.includes('name="Runner"'), "服务监控缺少 Supervisor、Scheduler、Planner、Dispatcher 或 Runner");
assert(source.capacity.includes("数据库") && source.capacity.includes("state.database"), "服务监控缺少数据库状态");
assert(source.server.includes('payload["database"]'), "状态 API 未提供数据库服务状态");
assert(source.server.includes('payload["scheduler_control"]') && source.types.includes("scheduler_control"), "状态 API 未提供 Scheduler 链自动化状态");
assert(source.capacity.includes("automation-button") && source.capacity.includes('const action = enabled ? "stop" : "start"') && source.capacity.includes("restart") && source.capacity.includes("trigger"), "服务监控缺少服务启停、重启、自动化按钮或单次触发控件");
assert(source.tasks.includes("scopeBlockGroups") && source.utilities.includes("blocked_by_task_ids"), "任务表未展示范围锁等待");
assert(source.tasks.includes("scope_queue_position"), "任务表未展示 scope 队列位置");
assert(source.tasks.includes("dependencyIndicatorState"), "任务表缺少依赖状态灯");
assert(source.tasks.includes("PREFLIGHT_LABELS"), "DRAFT 行缺少预检状态");
assert(source.tasks.includes("主/子状态") && source.tasks.includes("phase-stack"), "任务表缺少主状态与子状态列");
assert(source.tasks.includes("心跳超时") && source.tasks.includes("formatDuration"), "任务表缺少 heartbeat 或耗时展示");
assert(source.tasks.includes("copyFeedback") && source.tasks.includes("复制失败"), "复制任务 ID 缺少结果反馈");
assert(source.utilities.includes("contextualFilterValues") && source.app.includes("resetInvalidFilters"), "任务筛选未实现联动候选项和失效值重置");
assert(source.tasks.includes("executionConfigLabel"), "能力等级缺少执行配置说明");
assert(source.detail.includes("Operator 原始定义"), "任务详情缺少 Operator 原始定义");
assert(source.detail.includes("Planner 预检"), "任务详情缺少 Planner 预检");
assert(source.detail.includes("技术验收补充"), "任务详情缺少技术验收补充");
assert(source.detail.includes("预检证据"), "任务详情缺少预检证据");
assert(source.detail.includes("PlannerSplitSuggestions"), "任务详情缺少结构化拆分建议");
assert(source.detail.includes("可选方案"), "任务详情缺少人工介入选项");
assert(source.detail.includes("PendingBlockers"), "任务详情缺少结构化阻塞信息");
assert(source.detail.includes("ResultDiagnostic"), "任务详情缺少安全诊断字段投影");
assert(source.detail.includes("item.to") && source.detail.includes("item.reason"), "任务详情未按 API 契约展示状态历史");
assert(source.secrets.includes("SecretStore"), "缺少 Provider 密钥管理");
assert(source.secrets.includes("连接验证"), "SecretStore 缺少连接验证动作");
assert(source.secrets.includes("trapFocus") && source.secrets.includes("previousFocusRef"), "SecretStore 抽屉缺少焦点闭环");
assert(source.secrets.includes('provider.status !== "storage_unavailable"'), "SecretStore 存储不可用时仍暴露操作");
assert(source.secrets.includes('provider.validation_scope === "connection"'), "SecretStore 未显示验证范围");
assert(source.server.includes('BASE_DIR / "client" / "dist" / "index.html"'), "Dashboard Server 未固定使用 React 构建入口");
assert(!source.server.includes('BASE_DIR / "client" / "dashboard.html"'), "Dashboard Server 仍包含旧页面回退");
assert(!source.server.includes('"/dashboard.html"'), "Dashboard Server 仍保留旧页面 URL");

assert(/@media \(max-width: 700px\)/.test(source.styles), "Dashboard 缺少移动端布局");
assert(source.styles.includes("overflow-wrap: anywhere"), "长路径缺少安全换行");
assert(source.styles.includes("grid-template-columns: repeat(8, minmax(76px, 1fr))"), "顶部任务统计未保持八列");
assert(source.styles.includes("tbody tr { display: grid"), "窄视图任务表未转换为稳定卡片行");

assert(/<meta\s+charset=["']UTF-8["']/i.test(source.dist), "构建入口缺少 UTF-8 声明");
assert(/<script[^>]+src=["']\/assets\/[^"']+\.js["']/.test(source.dist), "构建入口未引用本地 JavaScript 产物");
assert(/<link[^>]+href=["']\/assets\/[^"']+\.css["']/.test(source.dist), "构建入口未引用本地 CSS 产物");
assert(!/https?:\/\//i.test(source.dist), "构建入口不应依赖外部 CDN");
for (const match of source.dist.matchAll(/["'](\/assets\/[^"']+)["']/g)) {
  await access(resolve(distRoot, match[1].slice(1)), constants.R_OK).catch(() => errors.push(`构建资源不存在：${match[1]}`));
}
let legacyDashboardExists = true;
await access(resolve(clientRoot, "dashboard.html"), constants.F_OK).catch(() => { legacyDashboardExists = false; });
assert(!legacyDashboardExists, "旧 client/dashboard.html 不应继续存在");

assert(source.operationsHtml.includes('href="/operations.css"'), "运维页面未加载专用样式");
assert(source.operationsHtml.includes('src="/operations.js"'), "运维页面未加载专用脚本");
assert(source.operationsHtml.includes('href="/" aria-label="返回任务面板"'), "运维页面缺少返回任务面板入口");
assert(source.operationsJavaScript.includes('const OPERATIONS_ENDPOINT = "/api/operations-config"'), "运维页面未使用专用配置接口");
assert(source.operationsJavaScript.includes("textContent"), "运维页面未使用安全文本渲染");
assert(!/secret_ref|authorization|hidden_reasoning|response_body/i.test(source.operationsHtml + source.operationsJavaScript), "运维页面包含敏感字段名称");
assert(/@media \(max-width: 560px\)/.test(source.operationsCss), "运维页面缺少窄视图布局");
assert(source.server.includes('RUNTIME_LOGS_API_PATH = "/api/runtime-logs"'), "Dashboard Server 缺少运行日志 API");
assert(source.server.includes("RUNTIME_LOG_FILENAMES"), "运行日志 API 未固定日志来源");
assert(source.runtimeLogsHtml.includes('href="/runtime-logs.css"') && source.runtimeLogsHtml.includes('src="/runtime-logs.js"'), "运行日志页面未加载专用资源");
assert(source.runtimeLogsHtml.includes('href="/" aria-label="返回任务面板"'), "运行日志页面缺少返回任务面板入口");
assert(source.runtimeLogsJavaScript.includes('const RUNTIME_LOGS_ENDPOINT = "/api/runtime-logs"'), "运行日志页面未使用专用 API");
assert(source.runtimeLogsJavaScript.includes("textContent") && source.runtimeLogsJavaScript.includes("window.setInterval"), "运行日志页面缺少安全渲染或轮询");
assert(/@media \(max-width: 560px\)/.test(source.runtimeLogsCss), "运行日志页面缺少窄视图布局");
assert(source.header.includes('href="/runtime-logs.html"'), "主面板缺少运行日志入口");

if (errors.length) {
  console.error(`Dashboard 检查失败，共 ${errors.length} 项：`);
  errors.forEach((error, index) => console.error(`${index + 1}. ${error}`));
  process.exitCode = 1;
} else {
  console.log("Dashboard 检查通过：React/TypeScript 源码、Vite 本地构建和运维页面契约有效");
}
