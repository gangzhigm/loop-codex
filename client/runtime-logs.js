const RUNTIME_LOGS_ENDPOINT = "/api/runtime-logs";
const POLL_INTERVAL_MS = 5000;
const labels = { planner: "Planner", dispatcher: "Dispatcher", runner: "Runner" };
const tabs = [...document.querySelectorAll("#log-tabs button")];
const entriesElement = document.querySelector("#entries");
const statusElement = document.querySelector("#status");
const updatedAtElement = document.querySelector("#updated-at");
const sourceTitleElement = document.querySelector("#source-title");
const sourceMetaElement = document.querySelector("#source-meta");
const entryCountElement = document.querySelector("#entry-count");
let activeSource = "planner";
let payload = null;

function text(value, fallback = "--") {
  return value === null || value === undefined || value === "" ? fallback : String(value);
}

function detailText(details) {
  const fields = ["outcome", "task_id", "execution_id", "queued_count", "message", "error_type", "status"];
  return fields.filter((field) => details[field] !== undefined).map((field) => `${field}: ${text(details[field])}`).join(" | ");
}

function render() {
  tabs.forEach((tab) => {
    const active = tab.dataset.source === activeSource;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  const log = payload?.logs?.[activeSource];
  sourceTitleElement.textContent = labels[activeSource];
  entriesElement.replaceChildren();
  if (!log) {
    sourceMetaElement.textContent = "日志未返回";
    entryCountElement.textContent = "";
    return;
  }
  sourceMetaElement.textContent = log.available ? `${text(log.source)} · 最近读取 ${text(log.updated_at)}` : text(log.message, "日志不可用");
  const entries = Array.isArray(log.entries) ? log.entries : [];
  entryCountElement.textContent = `${entries.length} 条`;
  if (!entries.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = log.available ? "还没有可显示的运行记录。" : text(log.message, "日志不可用");
    entriesElement.append(empty);
    return;
  }
  [...entries].reverse().forEach((entry) => {
    const item = document.createElement("article");
    item.className = "log-entry";
    const header = document.createElement("div");
    header.className = "log-entry-header";
    const event = document.createElement("strong");
    event.textContent = text(entry.event, "运行记录");
    const time = document.createElement("time");
    time.textContent = text(entry.at, "无时间戳");
    header.append(event, time);
    const body = document.createElement("pre");
    body.textContent = entry.details ? (detailText(entry.details) || JSON.stringify(entry.details, null, 2)) : text(entry.message);
    item.append(header, body);
    entriesElement.append(item);
  });
}

async function load() {
  try {
    const response = await fetch(RUNTIME_LOGS_ENDPOINT, { headers: { Accept: "application/json" }, cache: "no-store" });
    const next = await response.json();
    if (!response.ok || next.ok !== true || !next.logs || typeof next.logs !== "object") throw new Error("日志响应无效");
    payload = next;
    statusElement.className = "status";
    statusElement.textContent = "";
    updatedAtElement.textContent = `最近读取：${text(next.generated_at)}`;
    render();
  } catch {
    statusElement.className = "status error";
    statusElement.textContent = "无法读取本机运行日志，页面保留最后一次有效记录。";
    updatedAtElement.textContent = "读取失败";
  }
}

tabs.forEach((tab) => tab.addEventListener("click", () => { activeSource = tab.dataset.source; render(); }));
load();
window.setInterval(() => void load(), POLL_INTERVAL_MS);
