import { parseBibtex } from "../importers/bibtex.js";
import { parseCsv } from "../importers/csv.js";
import { parseDoiText } from "../importers/doi.js";
import { DEFAULT_MAX_PDF_BYTES, ITEM_STATE, MESSAGE } from "../shared/constants.js";
import {
  abandonItem,
  chooseDestination,
  downloadBridgeInstaller,
  getState,
  importRecords,
  savePolarisConnection,
  testPolarisConnection,
  openBridgeInstaller,
  openCachedPdf,
  openManualPage,
  pairZotero,
  refreshBridgeStatus,
  refreshZoteroStatus,
  reparseItem,
  retryZoteroItem,
  retryZoteroPending,
  resumeTask,
  retryItem,
  showBridgeInstaller,
  startCaching,
  startAssistedPdfCapture,
  startDownloads,
  startRegistration,
  stopTask,
  stopAssistedPdfCapture,
  updateSettings,
  updateZoteroSettings,
  disconnectZotero,
} from "./actions.js";
import {
  renderActivityLog,
  renderCurrentAction,
  renderDownloadSummary,
  renderJobSelect,
  renderPipelineOverview,
  renderPublisherGroups,
  renderStorageMeter,
  renderSummary,
} from "./render.js";
import { defaultSelectedItemIds, reconcileSelectedItemIds, unsuccessfulItemIds } from "./selection.js";

const dom = Object.fromEntries([
  "bridge-status", "resume-task", "stop-task", "current-task-label", "job-select", "import-topic", "doi-input", "import-doi", "file-input", "file-name",
  "start-registration", "summary-grid", "selected-count", "publisher-groups", "destination-path", "choose-destination",
  "naming-template", "max-pdf-mb", "save-settings", "download-summary", "start-downloads", "notice",
  "pipeline-overview", "storage-meter", "current-action", "activity-log", "activity-count",
  "bridge-setup", "bridge-setup-title", "bridge-setup-description", "bridge-install-progress",
  "download-bridge-installer", "open-bridge-installer", "show-bridge-installer", "refresh-bridge-status",
  "zotero-setup", "zotero-setup-title", "zotero-setup-description", "zotero-pairing", "zotero-pairing-code",
  "pair-zotero", "zotero-auto-sync", "retry-zotero-pending", "disconnect-zotero", "refresh-zotero-status",
  "polaris-origin", "polaris-api-key", "test-polaris", "save-polaris", "polaris-status",
].map((id) => [id, document.getElementById(id)]));

let state = {
  jobs: [],
  currentTaskId: null,
  snapshot: null,
  bridge: { connected: false },
  bridgeInstaller: { state: "not-started", progress: 0 },
  zotero: { available: false, paired: false, autoSync: true },
  polarisConnection: null,
};
let selectedIds = new Set();
let selectionTaskId = null;
const selectionsByTask = new Map();
const initializedTaskIds = new Set();
let noticeTimer;
let refreshTimer;
let refreshRevision = 0;
let bridgePollTimer;
let bridgePollAttempts = 0;
const autoOpenedTasks = new Set();

function activateTab(name) {
  for (const item of document.querySelectorAll("[data-tab]")) item.classList.toggle("active", item.dataset.tab === name);
  for (const panel of document.querySelectorAll(".tab-panel")) panel.classList.toggle("active", panel.id === `tab-${name}`);
}

function showNotice(message, error = false) {
  clearTimeout(noticeTimer);
  dom.notice.textContent = message;
  dom.notice.className = `notice visible${error ? " error" : ""}`;
  noticeTimer = setTimeout(() => { dom.notice.className = "notice"; }, 5000);
}

function selectDefaults(taskId, items, origin = null) {
  if (!taskId) {
    selectionTaskId = null;
    selectedIds = new Set();
    return;
  }
  if (selectionTaskId !== taskId) {
    if (selectionTaskId) selectionsByTask.set(selectionTaskId, selectedIds);
    selectionTaskId = taskId;
    selectedIds = selectionsByTask.get(taskId) || new Set();
  }
  const initializeDefaults = !initializedTaskIds.has(taskId);
  selectedIds = reconcileSelectedItemIds(items, selectedIds, initializeDefaults, origin);
  if (items.length) initializedTaskIds.add(taskId);
  selectionsByTask.set(taskId, selectedIds);
}

function render() {
  const { jobs, currentTaskId, snapshot, bridge, bridgeInstaller = {}, zotero = {}, polarisConnection } = state;
  renderJobSelect(dom["job-select"], jobs, currentTaskId);
  const bridgeReady = Boolean(bridge.connected && bridge.compatible);
  dom["bridge-status"].textContent = bridgeReady
    ? `本地桥 ${bridge.version}`
    : bridge.connected ? `本地桥 ${bridge.version} 需更新` : "纯插件模式";
  dom["bridge-status"].className = `status-indicator${bridgeReady ? " ok" : ""}`;
  dom["stop-task"].hidden = !["registering", "caching", "archiving", "stopping"].includes(snapshot?.job?.status);
  dom["stop-task"].disabled = snapshot?.job?.status === "stopping";
  dom["stop-task"].textContent = snapshot?.job?.status === "stopping" ? "正在停止" : "停止";
  dom["resume-task"].hidden = !["stopped", "paused"].includes(snapshot?.job?.status);
  const installerComplete = bridgeInstaller.state === "complete";
  const installerRunning = bridgeInstaller.state === "in_progress";
  dom["bridge-setup"].classList.toggle("connected", bridgeReady);
  dom["bridge-setup-title"].textContent = bridgeReady ? "本地下载桥已连接" : bridge.connected ? "更新本地下载桥" : "安装本地下载桥";
  dom["bridge-setup-description"].textContent = bridgeReady
    ? `版本 ${bridge.version || "unknown"}，可选择任意目录并执行严格 PDF 校验。`
    : bridge.connected
      ? `当前版本 ${bridge.version || "unknown"} 不支持新的 PDF 严格验收流程，请覆盖安装最新版。`
    : installerComplete
      ? "安装包已下载。打开后确认 Windows 提示，安装完成会自动复检连接。"
      : installerRunning
        ? `正在下载安装程序 ${bridgeInstaller.progress || 0}%`
        : bridgeInstaller.error
          ? `下载失败：${bridgeInstaller.error}。请重新下载安装程序；PDF 缓存不会写入浏览器下载目录。`
          : "缓存可直接使用；最终归档需要本地桥选择目录并执行严格 PDF 校验。";
  dom["bridge-install-progress"].hidden = !installerRunning;
  dom["bridge-install-progress"].value = Number(bridgeInstaller.progress || 0);
  dom["download-bridge-installer"].hidden = Boolean(bridgeReady || installerComplete);
  dom["download-bridge-installer"].disabled = installerRunning;
  dom["download-bridge-installer"].textContent = installerRunning ? `下载中 ${bridgeInstaller.progress || 0}%` : "下载安装程序";
  dom["open-bridge-installer"].hidden = Boolean(bridgeReady || !installerComplete);
  dom["show-bridge-installer"].hidden = Boolean(bridgeReady || !installerComplete);
  const zoteroReady = Boolean(zotero.available && zotero.paired);
  dom["zotero-setup"].classList.toggle("connected", zoteroReady);
  dom["zotero-setup-title"].textContent = zoteroReady
    ? `Zotero Companion ${zotero.pluginVersion || ""}`.trim()
    : zotero.available ? "连接 Zotero Companion" : "未检测到 Zotero Companion";
  dom["zotero-setup-description"].textContent = zoteroReady
      ? "归档 PDF 将按 DOI 优先规则复制到 Zotero 存储；身份冲突会被拒绝。"
    : zotero.available
      ? zotero.pairingAvailable
        ? "Zotero 正在等待配对。输入 Zotero 窗口中显示的 6 位一次性配对码。"
        : "请在 Zotero 的 YFR 文献检索窗口生成一次性配对码。"
      : "请启动 Zotero 并安装 YFR Zotero Companion 0.3.0 或更高版本。";
  dom["zotero-pairing"].hidden = !zotero.available || zoteroReady;
  dom["zotero-auto-sync"].checked = zotero.autoSync !== false;
  dom["retry-zotero-pending"].disabled = !zoteroReady;
  dom["disconnect-zotero"].hidden = !zoteroReady;
  if (polarisConnection?.instanceOrigin && document.activeElement !== dom["polaris-origin"]) {
    dom["polaris-origin"].value = polarisConnection.instanceOrigin;
  }
  if (!dom["polaris-api-key"].value) {
    dom["polaris-api-key"].placeholder = polarisConnection ? "API Key 已安全保存" : "输入用户专属 API Key";
  }
  dom["polaris-status"].textContent = polarisConnection
    ? `已连接 · ${polarisConnection.user?.email || "用户"}`
    : "尚未连接";
  dom["current-task-label"].textContent = snapshot?.job?.origin?.topic || snapshot?.job?.taskCode || "尚未选择任务";
  const items = snapshot?.items || [];
  selectDefaults(currentTaskId, items, snapshot?.job?.origin);
  renderSummary(dom["summary-grid"], items);
  renderPipelineOverview(dom["pipeline-overview"], items);
  renderStorageMeter(dom["storage-meter"], state.storage, snapshot?.job?.limits?.maxPdfBytes || DEFAULT_MAX_PDF_BYTES);
  renderCurrentAction(dom["current-action"], items, snapshot?.job);
  renderActivityLog(dom["activity-log"], dom["activity-count"], snapshot?.logs || []);
  dom["selected-count"].textContent = `${selectedIds.size} 篇已选`;
  renderPublisherGroups(dom["publisher-groups"], items, selectedIds, {
    onToggle: (id) => { selectedIds.has(id) ? selectedIds.delete(id) : selectedIds.add(id); render(); },
    onOpen: async (id) => run(() => openManualPage(currentTaskId, id), "已打开出版社验证页面"),
    onRetry: async (id) => run(() => retryItem(currentTaskId, id), "已提交重新检测"),
    onReparse: async (id) => run(() => reparseItem(currentTaskId, id), "已从 DOI 重新解析"),
    onCache: async (id) => run(() => startCaching(currentTaskId, [id]), "已开始缓存并验真"),
    onViewCache: async (id) => run(() => openCachedPdf(currentTaskId, id), "已打开缓存 PDF"),
    onStartCapture: async (id) => run(
      () => startAssistedPdfCapture(currentTaskId, id),
      "已监听当前标签页；请在 120 秒内点击出版社的 PDF 或 Download 按钮",
    ),
    onStopCapture: async (id) => run(() => stopAssistedPdfCapture(currentTaskId, id), "已停止捕获 PDF"),
    onAbandon: async (id) => run(() => abandonItem(currentTaskId, id), "已记录放弃操作"),
    onRetryZotero: async (id) => run(() => retryZoteroItem(currentTaskId, id), "已重新提交 Zotero 同步"),
  });
  renderDownloadSummary(dom["download-summary"], items, selectedIds);
  const job = snapshot?.job;
  dom["destination-path"].textContent = job?.destination?.displayPath || "尚未选择归档目录";
  dom["naming-template"].value = job?.destination?.namingTemplate || "{taskCode}_{index}";
  dom["max-pdf-mb"].value = String(Math.round((job?.limits?.maxPdfBytes || DEFAULT_MAX_PDF_BYTES) / 1024 / 1024));
}

async function refresh(taskId = null) {
  const revision = ++refreshRevision;
  const nextState = await getState(taskId);
  if (revision !== refreshRevision) return;
  state = nextState;
  render();
  if (state.currentTaskId && !autoOpenedTasks.has(state.currentTaskId)) {
    autoOpenedTasks.add(state.currentTaskId);
    activateTab("process");
  }
}

function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => refresh().catch((error) => showNotice(error.message, true)), 180);
}

async function run(action, successMessage) {
  try {
    await action();
    if (successMessage) showNotice(successMessage);
    await refresh();
  } catch (error) {
    showNotice(error instanceof Error ? error.message : "操作失败", true);
  }
}

function pollBridgeAfterInstaller() {
  clearTimeout(bridgePollTimer);
  bridgePollAttempts = 0;
  const poll = async () => {
    bridgePollAttempts += 1;
    try {
      await refreshBridgeStatus();
      await refresh();
      if (state.bridge?.connected && state.bridge?.compatible) {
        showNotice("本地下载桥已连接");
        return;
      }
    } catch {
      // Keep polling while the user confirms the Windows installer.
    }
    if (bridgePollAttempts < 20) bridgePollTimer = setTimeout(poll, 1500);
  };
  bridgePollTimer = setTimeout(poll, 800);
}

for (const button of document.querySelectorAll("[data-tab]")) {
  button.addEventListener("click", () => activateTab(button.dataset.tab));
}

dom["job-select"].addEventListener("change", () => run(() => refresh(dom["job-select"].value)));
dom["import-doi"].addEventListener("click", () => run(async () => {
  const records = parseDoiText(dom["doi-input"].value);
  if (!records.length) throw new Error("没有识别到有效 DOI");
  await importRecords({ type: "doi-list", topic: dom["import-topic"].value.trim() || "DOI 文献任务" }, records);
  dom["doi-input"].value = "";
}, "DOI 已导入"));
dom["file-input"].addEventListener("change", () => run(async () => {
  const file = dom["file-input"].files?.[0];
  if (!file) return;
  dom["file-name"].textContent = file.name;
  const text = await file.text();
  const isCsv = file.name.toLowerCase().endsWith(".csv");
  const records = isCsv ? parseCsv(text) : parseBibtex(text);
  if (!records.length) throw new Error("文件中没有识别到文献记录");
  await importRecords({ type: isCsv ? "csv" : "bibtex", topic: dom["import-topic"].value.trim() || file.name }, records);
}, "文件已导入"));

dom["start-registration"].addEventListener("click", () => run(async () => {
  if (!state.currentTaskId) throw new Error("请先导入或选择任务");
  if (!selectedIds.size) throw new Error("请至少选择一篇文献");
  await startRegistration(state.currentTaskId, Array.from(selectedIds));
}, "逐篇处理已开始：当前文献完成后才会进入下一篇"));

dom["stop-task"].addEventListener("click", () => run(async () => {
  if (!state.currentTaskId) throw new Error("当前没有可停止的任务");
  await stopTask(state.currentTaskId);
}, "已提交停止请求"));

dom["resume-task"].addEventListener("click", () => run(async () => {
  if (!state.currentTaskId) throw new Error("当前没有可继续的任务");
  await resumeTask(state.currentTaskId);
}, "已继续 PDF 获取流程"));

for (const button of document.querySelectorAll("[data-select]")) {
  button.addEventListener("click", () => {
    const items = state.snapshot?.items || [];
    if (button.dataset.select === "all") selectedIds = new Set(items.map((item) => item.id));
    else if (button.dataset.select === "invert") selectedIds = new Set(items.filter((item) => !selectedIds.has(item.id)).map((item) => item.id));
    else if (button.dataset.select === "unsuccessful") selectedIds = unsuccessfulItemIds(items);
    else if (button.dataset.select === "clear") selectedIds = new Set();
    else selectedIds = defaultSelectedItemIds(items);
    if (state.currentTaskId) selectionsByTask.set(state.currentTaskId, selectedIds);
    render();
  });
}

dom["choose-destination"].addEventListener("click", () => run(async () => {
  if (!state.currentTaskId) throw new Error("请先选择任务");
  await chooseDestination(state.currentTaskId);
}, "目标目录已更新"));

function polarisConnection() {
  return {
    instanceOrigin: dom["polaris-origin"].value.trim(),
    apiKey: dom["polaris-api-key"].value.trim(),
  };
}

dom["test-polaris"].addEventListener("click", () => run(async () => {
  const result = await testPolarisConnection(polarisConnection());
  dom["polaris-status"].textContent = `连接正常 · ${result.user.email}`;
}, "Polaris 连接测试通过"));

dom["save-polaris"].addEventListener("click", () => run(async () => {
  const result = await savePolarisConnection(polarisConnection());
  dom["polaris-origin"].value = result.origin;
  dom["polaris-api-key"].value = "";
  dom["polaris-api-key"].placeholder = "API Key 已安全保存";
  dom["polaris-status"].textContent = `已连接 · ${result.user.email}`;
}, "Polaris 连接已保存"));

dom["download-bridge-installer"].addEventListener("click", () => run(
  downloadBridgeInstaller,
  "安装程序开始下载；完成后请打开并确认 Windows 提示",
));

dom["open-bridge-installer"].addEventListener("click", () => run(async () => {
  await openBridgeInstaller();
  pollBridgeAfterInstaller();
}, "安装程序已打开，请确认 Windows 提示"));

dom["show-bridge-installer"].addEventListener("click", () => run(
  showBridgeInstaller,
  "已在文件夹中显示安装程序",
));

dom["refresh-bridge-status"].addEventListener("click", () => run(
  refreshBridgeStatus,
  "本地桥状态已重新检测",
));

dom["pair-zotero"].addEventListener("click", () => run(async () => {
  const code = dom["zotero-pairing-code"].value.trim();
  await pairZotero(code);
  dom["zotero-pairing-code"].value = "";
}, "Zotero 配对成功，归档 PDF 将自动同步"));

dom["zotero-auto-sync"].addEventListener("change", () => run(
  () => updateZoteroSettings(dom["zotero-auto-sync"].checked),
  dom["zotero-auto-sync"].checked ? "Zotero 自动同步已开启" : "Zotero 自动同步已关闭",
));

dom["retry-zotero-pending"].addEventListener("click", () => run(async () => {
  await retryZoteroPending(state.currentTaskId || null);
}, "当前任务的 Zotero 待同步项已重试"));

dom["disconnect-zotero"].addEventListener("click", () => run(
  disconnectZotero,
  "浏览器端配对凭据已清除",
));

dom["refresh-zotero-status"].addEventListener("click", () => run(
  refreshZoteroStatus,
  "Zotero 连接状态已重新检测",
));

dom["save-settings"].addEventListener("click", () => run(async () => {
  if (!state.currentTaskId) throw new Error("请先选择任务");
  const mb = Math.max(1, Math.min(150, Number(dom["max-pdf-mb"].value) || 150));
  await updateSettings(state.currentTaskId, { namingTemplate: dom["naming-template"].value.trim() || "{taskCode}_{index}" }, { maxPdfBytes: Math.floor(mb * 1024 * 1024) });
}, "设置已保存"));

dom["start-downloads"].addEventListener("click", () => run(async () => {
  if (!state.currentTaskId) throw new Error("请先选择任务");
  if (!selectedIds.size) throw new Error("请至少选择一篇文献");
  await startDownloads(state.currentTaskId, Array.from(selectedIds));
}, "归档队列已开始"));

chrome.runtime.onMessage.addListener((message) => {
  if (message?.type === MESSAGE.STATE_CHANGED) scheduleRefresh();
});

refresh().catch((error) => showNotice(error.message, true));
