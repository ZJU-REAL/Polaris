import { cachePdfBytes } from "../background/cache.js";
import { requestOriginPermission, requestUrlPermission } from "../background/bridge.js";
import { normalizePolarisOrigin } from "../shared/origin.js";

const originInput = document.querySelector("#polaris-origin");
const apiKeyInput = document.querySelector("#polaris-api-key");
const connectionBadge = document.querySelector("#connection-badge");
const connectionStatus = document.querySelector("#connection-status");
const taskList = document.querySelector("#task-list");
const emptyState = document.querySelector("#empty-state");
const notice = document.querySelector("#notice");

const STATUS_LABELS = {
  queued: "待下载",
  downloading: "下载中",
  cached: "已缓存",
  archiving: "归档中",
  archived: "已归档",
  failed: "需要处理",
};

let state = { connection: null, tasks: [] };
let noticeTimer = null;

function showNotice(message, error = false) {
  notice.textContent = message;
  notice.className = `notice visible${error ? " error" : ""}`;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { notice.className = "notice"; }, 4500);
}

async function send(type, payload = null) {
  const response = await chrome.runtime.sendMessage({ type, payload });
  if (!response?.ok) throw new Error(response?.error || "扩展操作失败");
  return response;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function button(label, action, dataset = {}, className = "secondary compact") {
  const node = element("button", className, label);
  node.type = "button";
  node.dataset.action = action;
  Object.assign(node.dataset, dataset);
  return node;
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (!value) return "";
  if (value < 1024 * 1024) return `${Math.ceil(value / 1024)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function renderConnection() {
  const connected = Boolean(state.connection?.hasApiKey);
  connectionBadge.textContent = connected ? "已连接" : "未连接";
  connectionBadge.className = `badge${connected ? " connected" : ""}`;
  connectionStatus.textContent = connected
    ? state.connection.user?.email || "API Key 已验证"
    : "尚未配置";
  if (state.connection?.instanceOrigin && !originInput.value) originInput.value = state.connection.instanceOrigin;
  apiKeyInput.placeholder = connected ? "已保存，留空可继续使用" : "pol_dl_...";
}

function renderItem(task, item) {
  const row = element("article", "paper-item");
  const top = element("div", "paper-top");
  const title = element("h3", "paper-title", item.title);
  const status = element("span", `status-chip ${item.status}`, STATUS_LABELS[item.status] || item.status);
  top.append(title, status);
  row.append(top);

  const meta = element("div", "item-meta");
  if (item.doi) meta.append(element("span", "", `DOI ${item.doi}`));
  if (item.cache?.byteSize) meta.append(element("span", "", formatBytes(item.cache.byteSize)));
  if (item.cache?.sha256) meta.append(element("span", "", `SHA-256 ${item.cache.sha256.slice(0, 10)}...`));
  row.append(meta);

  if (item.error) row.append(element("p", "item-error", item.error));

  if (item.candidates.length) {
    const candidateRow = element("div", "candidate-row");
    const select = element("select", "candidate-select");
    select.dataset.taskId = task.id;
    select.dataset.itemId = item.id;
    item.candidates.forEach((candidate, index) => {
      const option = element("option", "", `${candidate.source} · ${new URL(candidate.url).hostname}`);
      option.value = candidate.url;
      if (index === 0) option.selected = true;
      select.append(option);
    });
    candidateRow.append(select, button("下载候选 PDF", "download", { taskId: task.id, itemId: item.id }));
    row.append(candidateRow);
  }

  const actions = element("div", "item-actions");
  if (item.articleUrl) actions.append(button("打开论文页", "open", { url: item.articleUrl }));
  const fileInput = element("input", "file-input");
  fileInput.type = "file";
  fileInput.accept = "application/pdf,.pdf";
  fileInput.dataset.taskId = task.id;
  fileInput.dataset.itemId = item.id;
  actions.append(button("选择本地 PDF", "choose-file", { taskId: task.id, itemId: item.id }), fileInput);
  if (item.cache?.cacheKey && item.status !== "archived") {
    actions.append(button("归档到 Polaris", "archive", { taskId: task.id, itemId: item.id }, "primary compact"));
  }
  row.append(actions);
  return row;
}

function renderTask(task) {
  const card = element("section", "task-card");
  const head = element("div", "task-head");
  const title = element("div", "task-title");
  title.append(
    element("h3", "", `批量任务 · ${task.items.length} 篇`),
    element("small", "", new Date(task.createdAt).toLocaleString("zh-CN")),
  );
  const archived = task.items.filter((item) => item.status === "archived").length;
  head.append(title, element("span", "status-chip", `${archived}/${task.items.length} 已归档`));
  card.append(head);

  const toolbar = element("div", "task-toolbar");
  toolbar.append(
    button("批量归档已缓存", "archive-task", { taskId: task.id }, "primary compact"),
    button("删除任务", "delete-task", { taskId: task.id }, "danger compact"),
  );
  card.append(toolbar);
  const papers = element("div", "paper-list");
  task.items.forEach((item) => papers.append(renderItem(task, item)));
  card.append(papers);
  return card;
}

function render() {
  renderConnection();
  taskList.replaceChildren(...state.tasks.map(renderTask));
  emptyState.hidden = state.tasks.length > 0;
}

async function refresh() {
  const response = await send("GET_STATE");
  state = { connection: response.connection, tasks: response.tasks };
  render();
}

async function withBusy(target, operation) {
  const previous = target.textContent;
  target.disabled = true;
  target.textContent = "处理中...";
  try {
    return await operation();
  } finally {
    target.disabled = false;
    target.textContent = previous;
  }
}

async function connect(save, target) {
  await withBusy(target, async () => {
    const origin = normalizePolarisOrigin(originInput.value || state.connection?.instanceOrigin);
    await requestOriginPermission(origin);
    const response = await send(save ? "SAVE_CONNECTION" : "TEST_CONNECTION", {
      instanceOrigin: origin,
      apiKey: apiKeyInput.value,
    });
    apiKeyInput.value = "";
    showNotice(`${save ? "已保存" : "连接成功"}：${response.user?.email || response.origin}`);
    await refresh();
  });
}

document.querySelector("#test-connection").addEventListener("click", (event) => {
  connect(false, event.currentTarget).catch((error) => showNotice(error.message, true));
});
document.querySelector("#save-connection").addEventListener("click", (event) => {
  connect(true, event.currentTarget).catch((error) => showNotice(error.message, true));
});
document.querySelector("#refresh-tasks").addEventListener("click", () => {
  refresh().catch((error) => showNotice(error.message, true));
});

taskList.addEventListener("click", async (event) => {
  const target = event.target.closest("button[data-action]");
  if (!target) return;
  const { action, taskId, itemId, url } = target.dataset;
  try {
    await withBusy(target, async () => {
      if (action === "open") await send("OPEN_ARTICLE", { url });
      if (action === "choose-file") {
        const input = target.parentElement.querySelector(`input[data-item-id="${CSS.escape(itemId)}"]`);
        input?.click();
      }
      if (action === "download") {
        const select = target.parentElement.querySelector("select");
        await requestUrlPermission(select.value);
        await send("DOWNLOAD_ITEM", { taskId, itemId, url: select.value });
        showNotice("PDF 已验真并缓存到浏览器");
      }
      if (action === "archive") {
        await send("ARCHIVE_ITEM", { taskId, itemId });
        showNotice("PDF 已归档到对应论文");
      }
      if (action === "archive-task") {
        const result = await send("ARCHIVE_TASK", { taskId });
        showNotice(`批量归档完成：${result.archived} 篇成功，${result.failed} 篇失败`, result.failed > 0);
      }
      if (action === "delete-task") {
        if (!confirm("删除该任务及浏览器中的 PDF 缓存？此操作不可恢复。")) return;
        await send("DELETE_TASK", { taskId });
        showNotice("任务和对应浏览器缓存已删除");
      }
    });
    await refresh();
  } catch (error) {
    showNotice(error.message, true);
    await refresh().catch(() => undefined);
  }
});

taskList.addEventListener("change", async (event) => {
  const input = event.target.closest("input[type=file]");
  if (!input?.files?.length) return;
  const file = input.files[0];
  try {
    const cache = await cachePdfBytes({
      taskId: input.dataset.taskId,
      itemId: input.dataset.itemId,
      value: await file.arrayBuffer(),
      sourceUrl: null,
    });
    await send("MANUAL_PDF_CACHED", {
      taskId: input.dataset.taskId,
      itemId: input.dataset.itemId,
      cache,
    });
    showNotice("本地 PDF 已验真并缓存到浏览器");
    await refresh();
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    input.value = "";
  }
});

chrome.storage.onChanged.addListener((_changes, areaName) => {
  if (areaName === "local") refresh().catch(() => undefined);
});

refresh().catch((error) => showNotice(error.message, true));
