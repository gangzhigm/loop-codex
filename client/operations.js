/*
 * 运维配置页控制器。
 *
 * 读取链路：load() -> GET /api/operations-config -> render() -> HTML template。
 * 写入链路：系统文件夹选择器 -> 本地候选路径 -> set_task_root + 明确确认字符串 -> 重新 load()。
 *
 * 本文件不直接读写 initialization.json、项目清单或 SQLite。配置项来源、生效方式、校验结果
 * 都由 Dashboard Server 返回；页面只负责安全展示和当前唯一可编辑项的受控提交。
 * 手工排查先看 load() 的响应形状，再看 render()；写操作依次检查 CSRF、request_id、action
 * 和服务端返回的 error，禁止绕过 runOperationsAction() 直接篡改 DOM 当作保存成功。
 */

// 一、服务端端点和固定 DOM。两个 API 都必须是当前 Dashboard Server 的同源路径。
const OPERATIONS_ENDPOINT = "/api/operations-config";
const OPERATIONS_ACTION_ENDPOINT = "/api/operations-config/action";
const sectionsElement = document.querySelector("#sections");
const navigationElement = document.querySelector("#section-nav");
const statusElement = document.querySelector("#status");
const updatedAtElement = document.querySelector("#updated-at");
const itemTemplate = document.querySelector("#item-template");
const taskRootEditor = document.querySelector("#task-root-editor");
const taskRootValue = document.querySelector("#task-root-value");
const selectTaskRootButton = document.querySelector("#select-task-root");
const saveTaskRootButton = document.querySelector("#save-task-root");
const taskRootMessage = document.querySelector("#task-root-message");
// 二、页面会话状态。CSRF token 只存在内存；selectedTaskRoot 只是保存前的候选值。
let csrfToken = "";
let selectedTaskRoot = "";

// 三、配置值渲染。数组项使用允许列表字段展开，避免把整个服务端对象无差别显示出来。
function text(value) {
  return value === null || value === undefined ? "未配置" : String(value);
}

function renderValue(container, value) {
  container.replaceChildren();
  if (!Array.isArray(value)) {
    container.textContent = text(value);
    return;
  }
  const list = document.createElement("div");
  list.className = "value-list";
  for (const entry of value) {
    const row = document.createElement("div");
    row.className = "value-row";
    const key = document.createElement("span");
    key.className = "value-key";
    const detail = document.createElement("span");
    if (entry === null || typeof entry !== "object") {
      key.textContent = "配置值";
      detail.textContent = text(entry);
    } else {
      key.textContent = [entry.environment, entry.provider, entry.level].filter(Boolean).join(" / ") || entry.id || entry.provider_id || entry.name || "状态";
      const fields = ["value", "name", "model", "reasoning", "attempt_timeout_seconds", "max_active_executions", "status", "backend", "configured", "action"];
      detail.textContent = fields.filter((field) => entry[field] !== undefined && field !== "name").map((field) => `${field}: ${text(entry[field])}`).join(" | ");
    }
    row.append(key, detail);
    list.append(row);
  }
  container.append(list);
}

// item.editable 必须由服务端明确给出 true，前端不会根据 label 或 source 猜测写权限。
function renderItem(item) {
  const fragment = itemTemplate.content.cloneNode(true);
  fragment.querySelector("h3").textContent = text(item.label);
  const state = fragment.querySelector(".state");
  state.hidden = item.state !== "planned";
  state.textContent = "计划中";
  state.classList.add("planned");
  const editButton = fragment.querySelector(".edit-button");
  editButton.hidden = item.editable !== true;
  if (item.editable === true) editButton.addEventListener("click", () => openTaskRootEditor(item.value));
  renderValue(fragment.querySelector(".value"), item.value);
  fragment.querySelector(".source").textContent = text(item.source);
  fragment.querySelector(".activation").textContent = text(item.activation);
  fragment.querySelector(".validation").textContent = text(item.validation);
  fragment.querySelector(".description").textContent = text(item.description);
  return fragment;
}

function newRequestId() {
  return crypto.randomUUID();
}

/**
 * 调用一次受控运维动作。
 * CSRF 防止跨站请求，request_id 由调用方生成用于幂等审计；HTTP 成功但 ok !== true 仍视为失败。
 */
async function runOperationsAction(payload) {
  if (!csrfToken) throw new Error("运维操作会话无效");
  const response = await fetch(OPERATIONS_ACTION_ENDPOINT, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok || result.ok !== true) throw new Error(result.error || "运维操作失败");
  return result;
}

function setTaskRootEditorMessage(message = "", isError = false) {
  taskRootMessage.textContent = message;
  taskRootMessage.classList.toggle("error", isError);
}

// 四、任务根目录编辑流程。打开时保存按钮保持禁用，只有系统选择器返回 SELECTED 才可提交。
function openTaskRootEditor(value) {
  selectedTaskRoot = text(value);
  taskRootValue.value = selectedTaskRoot;
  saveTaskRootButton.disabled = true;
  setTaskRootEditorMessage();
  taskRootEditor.showModal();
}

selectTaskRootButton.addEventListener("click", async () => {
  selectTaskRootButton.disabled = true;
  setTaskRootEditorMessage("正在打开本机文件夹选择器");
  try {
    const result = await runOperationsAction({ action: "select_task_root", request_id: newRequestId() });
    if (result.outcome === "SELECTED") {
      selectedTaskRoot = result.task_root;
      taskRootValue.value = selectedTaskRoot;
      saveTaskRootButton.disabled = false;
      setTaskRootEditorMessage();
    } else {
      setTaskRootEditorMessage();
    }
  } catch (error) {
    setTaskRootEditorMessage(error.message || "无法打开文件夹选择器", true);
  } finally {
    selectTaskRootButton.disabled = false;
  }
});

// 保存要求固定 confirmation，服务端仍会验证绝对路径、安全边界和写入目标。
saveTaskRootButton.addEventListener("click", async () => {
  saveTaskRootButton.disabled = true;
  setTaskRootEditorMessage("正在校验并保存");
  try {
    await runOperationsAction({
      action: "set_task_root",
      request_id: newRequestId(),
      task_root: selectedTaskRoot,
      confirmation: "SET_TASK_ROOT",
    });
    taskRootEditor.close();
    await load();
    statusElement.className = "status";
    statusElement.textContent = "全局任务工作区已更新；新任务将使用新的修改上界。";
  } catch (error) {
    setTaskRootEditorMessage(error.message || "无法保存全局任务工作区", true);
    saveTaskRootButton.disabled = false;
  }
});

// 五、配置目录渲染。每个 section 再按 current/planned 分组，数量来自同一响应快照。
function render(payload) {
  sectionsElement.replaceChildren();
  navigationElement.replaceChildren();
  for (const section of payload.sections) {
    const navLink = document.createElement("a");
    navLink.href = `#${section.id}`;
    navLink.textContent = section.title;
    navigationElement.append(navLink);
    const element = document.createElement("section");
    element.className = "section";
    element.id = section.id;
    const header = document.createElement("div");
    header.className = "section-header";
    const title = document.createElement("h2");
    title.textContent = section.title;
    const currentItems = section.items.filter((item) => item.state !== "planned");
    const plannedItems = section.items.filter((item) => item.state === "planned");
    const count = document.createElement("span");
    count.textContent = plannedItems.length ? `${currentItems.length} 项生效 · ${plannedItems.length} 项计划` : `${currentItems.length} 项生效`;
    header.append(title, count);
    element.append(header);
    for (const [state, items] of [["current", currentItems], ["planned", plannedItems]]) {
      if (!items.length) continue;
      const group = document.createElement("section");
      group.className = `settings-group ${state}`;
      const groupTitle = document.createElement("h3");
      groupTitle.textContent = state === "current" ? "当前生效" : "规划中";
      const grid = document.createElement("div");
      grid.className = "settings";
      items.forEach((item) => grid.append(renderItem(item)));
      group.append(groupTitle, grid);
      element.append(group);
    }
    sectionsElement.append(element);
  }
  updatedAtElement.textContent = `最近读取：${text(payload.generated_at)}`;
}

/**
 * 读取完整运维配置快照并更新 CSRF token。
 * 任何 HTTP、JSON 或 sections 形状错误都保留页面骨架并显示失败，不使用旧 token 继续写操作。
 */
async function load() {
  statusElement.className = "status";
  statusElement.textContent = "";
  try {
    const response = await fetch(OPERATIONS_ENDPOINT, { headers: { Accept: "application/json" }, cache: "no-store" });
    const payload = await response.json();
    if (!response.ok || payload.ok !== true || !Array.isArray(payload.sections)) throw new Error("配置目录响应无效");
    csrfToken = response.headers.get("X-CSRF-Token") || "";
    render(payload);
  } catch (error) {
    statusElement.className = "status error";
    statusElement.textContent = "无法读取本机运维配置。";
    updatedAtElement.textContent = "读取失败";
  }
}

// 六、页面启动只有一次主动读取；后续保存成功会再次调用 load() 获取服务端最终状态。
load();
