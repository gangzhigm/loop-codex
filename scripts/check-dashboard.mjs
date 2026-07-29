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
assert(html.includes('const TASK_SCHEMA_VERSION = "2.0.0"'), "Dashboard Schema 版本不是 2.0.0");
assert(html.includes("@media (max-width: 760px)"), "缺少移动端响应式规则");

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

for (const label of ["任务总览", "任务", "需要关注", "Agent", "最近活动", "状态历史"]) {
  assert(html.includes(label), `缺少界面区域：${label}`);
}

if (errors.length) {
  console.error(`监控页检查失败，共 ${errors.length} 项：`);
  errors.forEach((error, index) => console.error(`${index + 1}. ${error}`));
  process.exitCode = 1;
} else {
  console.log(`监控页检查通过：${ids.size} 个唯一 DOM id，SQLite API 轮询和内联脚本有效`);
}
