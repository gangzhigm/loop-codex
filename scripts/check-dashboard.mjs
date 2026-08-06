import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

const targetPath = resolve(process.argv[2] ?? "dashboard.html");
const html = await readFile(targetPath, "utf8");
const errors = [];

function assert(condition, message) {
  if (!condition) errors.push(message);
}

assert(/<meta\s+charset=["']utf-8["']/i.test(html.slice(0, 1024)), "缺少靠前的 UTF-8 声明");
assert(!/<script[^>]+src=/i.test(html), "Dashboard 不能依赖外部脚本");
assert(!/<link[^>]+(?:stylesheet|preload)/i.test(html), "Dashboard 不能依赖外部样式或预加载资源");
assert(html.includes('const STATE_ENDPOINT = "/api/state"'), "缺少状态 API");
assert(/fetch\(STATE_ENDPOINT/.test(html), "未从状态 API 读取数据");
assert(html.includes('const TASK_SCHEMA_VERSION = "3.7.0"'), "Schema 契约不正确");

for (const label of ["草稿", "需确认", "待执行", "执行中", "已结束", "已归档"]) {
  assert(html.includes(`>${label}</button>`) || html.includes(`>${label}</span>`), `缺少 ${label} 生命周期入口`);
}
assert(html.includes('DRAFT: "草稿"'), "缺少 DRAFT 状态");
assert(html.includes('NEEDS_REVIEW: "需确认"'), "缺少 NEEDS_REVIEW 状态");
assert(html.includes('const PREFLIGHT_LABELS = { UNINSPECTED: "待静态检查", INSPECTING: "静态检查中"'), "缺少 Planner 预检阶段标签");
assert(html.includes('currentFilter === "draft" && task.status !== "DRAFT"'), "草稿筛选没有精确匹配 DRAFT");
assert(html.includes('currentFilter === "review" && task.status !== "NEEDS_REVIEW"'), "需确认筛选没有精确匹配 NEEDS_REVIEW");
assert(html.includes('task.status === "DRAFT" ? `${STATUS_LABELS[task.status]} · ${PREFLIGHT_LABELS[task.preflight_status]}`'), "草稿未展示静态检查阶段");
assert(!html.includes('currentFilter === "attention"'), "旧的需关注筛选仍在使用");
assert(!html.includes('const ATTENTION_STATUSES'), "旧的需关注状态集合仍在使用");
assert(html.includes('const LEGACY_QUEUE_STATUSES = new Set(["WAITING_CONFLICT", "BLOCKED"])'), "旧冲突数据没有兼容展示");
assert(!html.includes('task?.status !== "WAITING_CONFLICT"'), "旧 WAITING_CONFLICT 仍是主要冲突渲染路径");

assert(html.includes('task?.operator_definition'), "Dashboard 未验证 Operator 原始定义");
assert(html.includes('task?.planner_supplement'), "Dashboard 未验证 Planner 补充");
assert(html.includes('function renderPlannerSupplement(task)'), "缺少 Planner 预检详情渲染");
assert(html.includes('Operator 原始定义'), "详情没有区分 Operator 原始定义");
assert(html.includes('Operator 业务验收'), "详情没有展示 Operator 业务验收");
assert(html.includes('Planner 预检'), "详情没有展示 Planner 预检");
assert(html.includes('预估能力等级'), "详情没有展示预估能力等级");
assert(html.includes('最终能力等级'), "详情没有展示最终能力等级");
assert(html.includes('精确 scope'), "详情没有展示精确 scope");
assert(html.includes('锁模式'), "详情没有展示锁模式");
assert(html.includes('技术验收补充'), "详情没有展示技术验收补充");
assert(html.includes('预检证据'), "详情没有展示预检证据");
assert(html.includes('function renderSplitSuggestions(suggestions)'), "缺少拆分建议展示");
assert(html.includes('等待 Operator 或用户决定；此处不会自动创建子任务。'), "拆分建议没有明确等待人工决定");

assert(html.includes('function scopeBlockGroups(task, tasks)'), "缺少 PENDING 范围锁队列归类");
assert(html.includes('task?.status !== "PENDING"'), "范围锁展示未限定 PENDING");
assert(html.includes('task.blocked_by_task_ids'), "未使用 API 的 blocked_by_task_ids");
assert(html.includes('task.blocked_scopes'), "未使用 API 的 blocked_scopes");
assert(html.includes('task.scope_queue_position'), "未使用 API 的 scope_queue_position");
assert(html.includes('task.blocked_by_task_ids ??= []'), "缺少旧 API 阻塞任务字段兼容");
assert(html.includes('task.blocked_scopes ??= []'), "缺少旧 API 阻塞范围字段兼容");
assert(html.includes('task.blocked_scope_keys ??= []'), "缺少旧 API scope key 字段兼容");
assert(html.includes('task.blocking_scopes ??= []'), "缺少旧 API 阻塞详情字段兼容");
assert(html.includes('task.scope_queue_position ??= null'), "缺少旧 API 队列位置字段兼容");
assert(html.includes('function renderPendingBlockers(task, tasks)'), "缺少依赖和范围锁分离的详情展示");
assert(html.includes('<h4>依赖等待</h4>'), "详情未单独展示依赖等待");
assert(html.includes('<h4>范围锁等待</h4>'), "详情未单独展示范围锁等待");
assert(html.includes('${renderPendingBlockers(task, state.tasks)}'), "详情未挂载待执行阻塞信息");

assert(html.includes('function resetHeaderFilters()'), "缺少状态切换时的筛选重置");
assert(html.includes('const nextFilter = ["draft", "review", "pending", "active", "closed", "archived"]'), "分类切换没有覆盖当前生命周期");
assert(html.includes('function resetInvalidHeaderFilters(tasks)'), "缺少自动轮询后的失效筛选重置");
assert(html.includes('if (activeHeaderFilter) renderHeaderFilterMenu();'), "自动刷新会破坏打开的表头筛选状态");
assert(/\.planner-meta\s*\{[\s\S]*?grid-template-columns:\s*repeat\(2, minmax\(0, 1fr\)\)/.test(html), "Planner 详情缺少稳定网格布局");
assert(/@media \(max-width: 760px\)[\s\S]*?\.planner-meta\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\)/.test(html), "窄视图 Planner 信息未折叠为单列");
assert(html.includes('overflow-wrap: anywhere'), "长路径缺少可换行展示");

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

if (errors.length) {
  console.error(`监控页检查失败，共 ${errors.length} 项：`);
  errors.forEach((error, index) => console.error(`${index + 1}. ${error}`));
  process.exitCode = 1;
} else {
  console.log(`监控页检查通过：${ids.size} 个唯一 DOM id，Planner 生命周期与队列阻塞展示有效`);
}
