import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

const targetPath = resolve(process.argv[2] ?? "dashboard.html");
const html = await readFile(targetPath, "utf8");
const errors = [];

function assert(condition, message) {
  if (!condition) errors.push(message);
}

assert(/<meta\s+charset=["']utf-8["']/i.test(html.slice(0, 1024)), "缺少靠前的 UTF-8 声明");
assert(!/<script[^>]+src=/i.test(html), "监控页不能依赖外部脚本");
assert(!/<link[^>]+(?:stylesheet|preload)/i.test(html), "监控页不能依赖外部样式或预加载资源");
assert(html.includes('const STATE_ENDPOINT = "/api/state"'), "缺少 SQLite 状态 API 地址");
assert(/fetch\(STATE_ENDPOINT/.test(html), "监控页未通过 API 读取状态");
assert(!html.includes("showOpenFilePicker"), "仍包含旧文件授权逻辑");
assert(!html.includes("TASKS.json"), "仍将 TASKS.json 暴露为运行时数据源");
assert(html.includes('WAITING_CONFLICT: "等待冲突"'), "缺少 WAITING_CONFLICT 状态");
assert(html.includes('CONFIRMED: "已确认"'), "缺少 CONFIRMED 状态");
assert(html.includes('const TASK_SCHEMA_VERSION = "3.2.0"'), "Dashboard Schema 版本不是 3.2.0");
for (const profile of ["routine", "standard", "advanced", "deep", "complex", "exceptional"]) {
  assert(new RegExp(`\\b${profile}:\\s*"`).test(html), `缺少 ${profile} 执行档位`);
}
assert(html.includes("const EXECUTION_PROFILES = Object.keys(PROFILE_LABELS);"), "执行档位列表未从标签配置生成");
assert(html.includes('const PRIORITY_ORDER = { blocker: 0, critical: 1, high: 2, medium: 3, low: 4 }'), "缺少五级优先级排序");
assert(html.includes("@media (max-width: 760px)"), "缺少移动端响应式规则");
assert(html.includes('id="projectFilter"'), "缺少项目筛选控件");
assert(html.includes('id="profileFilter"'), "缺少执行档位筛选控件");
assert(html.includes('data-filter="pending"'), "缺少待执行筛选入口");
assert(html.includes('currentFilter === "pending" && task.status !== "PENDING"'), "待执行筛选未严格匹配 PENDING 状态");
assert(html.includes('data-filter="human"'), "缺少需人工筛选入口");
assert(html.includes('const HUMAN_STATUSES = new Set(["DRAFT", "WAITING_HUMAN"])'), "需人工汇总未覆盖 DRAFT 和 WAITING_HUMAN");
assert(html.includes('if (currentFilter === "human" && !HUMAN_STATUSES.has(task.status)) return false;'), "需人工筛选未严格匹配人工状态");
assert(html.includes('data-filter="archived"'), "缺少已归档筛选入口");
assert(html.includes('function isArchived(task)'), "缺少独立 archived_at 判定函数");
assert(html.includes('return Boolean(task?.archived_at);'), "archived_at 缺失或 null 时未按未归档处理");
assert(html.includes('if (!archived) return false;'), "已归档筛选未严格匹配 archived_at 非空任务");
assert(html.includes('} else if (archived) {'), "默认和其他状态筛选未排除已归档任务");
assert(html.includes('const currentTasks = tasks.filter((task) => !isArchived(task));'), "顶部统计未使用未归档任务口径");
assert(html.includes('<div class="metric-label">当前任务</div>'), "顶部任务总数标签未明确未归档口径");
assert(html.includes('`已归档 ${visible.length} 项`'), "已归档视图未显示筛选后的归档数量");
assert(html.includes('归档于 ${escapeHtml(formatDate(task.archived_at, false))}'), "任务行未显示归档时间");
assert(html.includes('<span class="detail-label">归档时间</span>'), "任务详情未显示归档时间");
assert(/\.task-tools\s*\{[\s\S]*?flex-wrap:\s*nowrap;/.test(html), "任务筛选工具栏仍可能换行");
assert(/\.segments\s*\{[\s\S]*?white-space:\s*nowrap;/.test(html), "状态筛选组未禁止文字换行");
assert(/\.segments button\s*\{[\s\S]*?flex:\s*0 0 54px;[\s\S]*?width:\s*54px;/.test(html), "状态筛选按钮未使用稳定等宽尺寸");
assert(/@media \(max-width: 760px\)[\s\S]*?\.task-section\s*\{\s*overflow:\s*hidden;\s*\}/.test(html), "移动端任务区未隔离横向溢出");
assert(/@media \(max-width: 760px\)[\s\S]*?table\s*\{\s*table-layout:\s*auto;\s*\}/.test(html), "移动端表格仍使用桌面固定列布局");
assert(/@media \(max-width: 760px\)[\s\S]*?tbody tr\s*\{[\s\S]*?grid-template-columns:\s*repeat\(2, minmax\(0, 1fr\)\);/.test(html), "移动端任务行未使用稳定两列网格");
assert(/@media \(max-width: 760px\)[\s\S]*?td\s*>\s*\*\s*\{[\s\S]*?max-width:\s*100%;/.test(html), "移动端任务字段值未限制在视口内");
assert(html.includes("task.scope_keys"), "未使用任务 scope_keys 推导项目");
assert(html.includes('data-label="项目"'), "任务列表缺少项目列");
assert(html.includes('const IMAGE_EXTENSIONS = new Set('), "缺少可预览图片类型白名单");
assert(html.includes('function renderAttachments(task)'), "缺少任务附件预览渲染");
assert(html.includes('/api/attachment?'), "缺少附件图片 API 地址");
assert(html.includes('function conflictGroups(task, tasks)'), "缺少冲突任务分组逻辑");
assert(html.includes('task?.status !== "WAITING_CONFLICT"'), "冲突提示未受 WAITING_CONFLICT 状态约束");
assert(html.includes('tasksById.get(blockerId)'), "未通过 blocker_task_id 映射阻塞任务标题");
assert(html.includes('group.scopes.has(scopeKey)'), "未按阻塞任务汇总并去重冲突项目");
assert(html.includes('function renderConflictIndicator(task, tasks)'), "任务行缺少阻塞任务摘要");
assert(html.includes('function renderConflictDetails(task, tasks)'), "任务详情缺少完整阻塞信息");
assert(html.includes('class="conflict-popover"'), "缺少可悬浮或聚焦查看的冲突详情");
assert((html.match(/event\.target\.closest\("\.conflict-summary"\)/g) ?? []).length === 2, "冲突摘要未同时隔离鼠标和键盘任务行事件");
assert(html.includes('阻塞任务标题未找到'), "缺少阻塞任务标题映射失败降级文案");
assert(html.includes('阻塞任务 ID 未记录'), "缺少冲突字段不完整降级文案");
assert(html.includes('检测时间无效'), "缺少无效冲突时间降级文案");

assert(html.includes('function copyTaskId(taskId, button)'), "缺少任务 ID 复制处理函数");
assert(html.includes('await navigator.clipboard.writeText(taskId)'), "复制操作未使用当前任务原始 ID");
assert(html.includes('data-copy-task-id="${escapeHtml(task.id)}"'), "任务行复制按钮未绑定完整 task.id");
assert(html.includes('aria-label="复制任务 ID"'), "复制按钮缺少可访问名称");
assert(html.includes('title="复制任务 ID"'), "复制按钮缺少提示文本");
assert(html.includes('copyButton.dataset.copyTaskId'), "复制事件未读取当前行绑定的任务 ID");
assert(html.includes('event.target.closest(".task-copy-button")'), "复制按钮未隔离任务行点击和键盘行为");
assert(html.includes('setFeedback("success", "已复制")'), "复制成功缺少反馈");
assert(html.includes('setFeedback("failed", "复制失败")'), "复制失败缺少反馈");
assert(/\.task-copy-button\s*\{[\s\S]*?width:\s*28px;[\s\S]*?height:\s*28px;/.test(html), "复制按钮未使用固定点击区域");
assert(/\.task-title-line\s*\{[\s\S]*?grid-template-columns:\s*minmax\(0, 1fr\) 28px;/.test(html), "标题和复制按钮未使用稳定布局");
assert(html.includes('elements.alertCount.textContent = `${alerts.length} 项`;'), "需要关注标题计数未保留全部任务数量");
assert(html.includes('alerts.slice(0, 1).map((alert) => `'), "需要关注摘要未限制为确定性首条任务");

const allIds = [...html.matchAll(/\sid=["']([^"']+)["']/g)].map((match) => match[1]);
const ids = new Set(allIds);
assert(ids.size === allIds.length, "HTML 中存在重复 id");
for (const match of html.matchAll(/querySelector\(["']#([^"']+)["']\)/g)) {
  assert(ids.has(match[1]), `脚本引用了不存在的 #${match[1]}`);
}

const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)];
assert(scripts.length === 1, `预期 1 个内联脚本，实际 ${scripts.length} 个`);
if (scripts.length === 1) {
  try {
    new Function(scripts[0][1]);
  } catch (error) {
    errors.push(`内联 JavaScript 语法错误：${error instanceof Error ? error.message : String(error)}`);
  }
}

for (const label of ["任务总览", "任务", "需要关注", "活动执行", "最近活动", "状态历史"]) {
  assert(html.includes(label), `缺少界面区域：${label}`);
}

if (errors.length) {
  console.error(`监控页检查失败，共 ${errors.length} 项：`);
  errors.forEach((error, index) => console.error(`${index + 1}. ${error}`));
  process.exitCode = 1;
} else {
  console.log(`监控页检查通过：${ids.size} 个唯一 DOM id，SQLite API 轮询和内联脚本有效`);
}
