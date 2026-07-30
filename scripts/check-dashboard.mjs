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
assert(html.includes('const TASK_SCHEMA_VERSION = "3.0.0"'), "Dashboard Schema 版本不是 3.0.0");
assert(html.includes("@media (max-width: 760px)"), "缺少移动端响应式规则");
assert(html.includes('id="projectFilter"'), "缺少项目筛选控件");
assert(html.includes('data-filter="pending"'), "缺少待执行筛选入口");
assert(html.includes('currentFilter === "pending" && task.status !== "PENDING"'), "待执行筛选未严格匹配 PENDING 状态");
assert(html.includes('data-filter="archived"'), "缺少已归档筛选入口");
assert(html.includes('currentFilter === "archived" && task.status !== "CONFIRMED"'), "已归档筛选未严格匹配 CONFIRMED 状态");
assert(/\.task-tools\s*\{[\s\S]*?flex-wrap:\s*nowrap;/.test(html), "任务筛选工具栏仍可能换行");
assert(/\.segments\s*\{[\s\S]*?white-space:\s*nowrap;/.test(html), "状态筛选组未禁止文字换行");
assert(/\.segments button\s*\{[\s\S]*?flex:\s*0 0 54px;[\s\S]*?width:\s*54px;/.test(html), "状态筛选按钮未使用稳定等宽尺寸");
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
