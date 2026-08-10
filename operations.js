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
let csrfToken = "";
let selectedTaskRoot = "";

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

load();
