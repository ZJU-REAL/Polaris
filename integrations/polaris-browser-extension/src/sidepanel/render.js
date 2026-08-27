import { groupByPublisher, itemCounts, stateLabel, stateTone, formatBytes } from "./format.js";
import { ITEM_STATE } from "../shared/constants.js";

const ABANDONABLE_STATES = new Set([
  ITEM_STATE.PENDING,
  ITEM_STATE.CANDIDATE_REGISTERED,
  ITEM_STATE.PDF_RESPONSE_VERIFIED,
  ITEM_STATE.PDF_CACHED,
  ITEM_STATE.AUTHORIZED,
  ITEM_STATE.LOGIN_REQUIRED,
  ITEM_STATE.MANUAL_REQUIRED,
  ITEM_STATE.NO_ENTITLEMENT,
  ITEM_STATE.BLOCKED,
  ITEM_STATE.FAILED,
  ITEM_STATE.INVALID_RESPONSE,
  ITEM_STATE.VERIFICATION_INCONCLUSIVE,
  ITEM_STATE.QUARANTINED,
]);

function element(tag, { className = "", text = "", attrs = {} } = {}) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = text;
  for (const [key, value] of Object.entries(attrs)) {
    if (value != null) node.setAttribute(key, String(value));
  }
  return node;
}

export function renderJobSelect(select, jobs, currentTaskId) {
  select.replaceChildren();
  if (!jobs.length) select.append(element("option", { text: "暂无任务", attrs: { value: "" } }));
  for (const job of jobs) {
    const topic = job.origin?.topic || job.taskCode;
    const sourceLabel = job.origin?.area === "literature-search"
      ? "文献检索"
      : job.origin?.area === "exclusive-review" ? "专属综述" : job.origin?.area === "daily-review" ? "前沿综述" : "导入";
    const contextId = job.origin?.searchId || job.origin?.runId || job.taskCode;
    const count = Number.isInteger(job.origin?.selectedCount) ? ` · ${job.origin.selectedCount}篇` : "";
    const option = element("option", { text: `${topic} · ${sourceLabel} ${contextId}${count} · ${job.status}`, attrs: { value: job.id } });
    option.selected = job.id === currentTaskId;
    select.append(option);
  }
}

export function renderSummary(container, items) {
  const counts = itemCounts(items);
  container.replaceChildren();
  for (const [label, value] of [["总数", counts.total], ["PDF入口", counts.candidates], ["需人工", counts.manual], ["已下载", counts.completed]]) {
    const item = element("div", { className: "summary-item" });
    item.append(element("strong", { text: String(value) }), element("span", { text: label }));
    container.append(item);
  }
}

function zoteroStateMeta(sync = {}) {
  if (sync.state === "linked") {
    return sync.attachmentMode === "stored"
      ? ["已存入 Zotero", "good"]
      : ["Zotero 链接待迁移", "warn"];
  }
  return ({
    pending: ["Zotero 待同步", "warn"],
    syncing: ["正在同步 Zotero", "active"],
    failed: ["Zotero 同步失败", "bad"],
  })[sync.state] || null;
}

function renderPaper(item, selected, handlers) {
  const row = element("div", { className: "paper-item" });
  const checkbox = element("input", { attrs: { type: "checkbox", "aria-label": `选择 ${item.title}` } });
  checkbox.checked = selected;
  checkbox.addEventListener("change", () => handlers.onToggle(item.id));
  const body = element("div");
  body.append(element("p", { className: "paper-title", text: item.title }));
  const stableId = item.identifiers?.pii || item.identifiers?.ieeeDocumentNumber || item.doi || "无 DOI";
  const yfrId = item.yfrPaperId ? `YFR ${item.yfrPaperId} · ` : "";
  body.append(element("p", { className: "paper-meta", text: `${item.ordinal}. ${yfrId}${item.publisher || item.publisherKey || "待识别"} · ${stableId}` }));
  body.append(element("span", { className: `state-label ${stateTone(item.state)}`, text: stateLabel(item.state) }));
  if (item.statusReason) body.append(element("p", { className: "paper-meta", text: item.statusReason }));
  const progress = item.state === ITEM_STATE.CACHING ? item.cacheProgress : item.state === ITEM_STATE.ARCHIVING ? item.archiveProgress : null;
  if (progress) {
    const progressRow = element("div", { className: "paper-progress" });
    const bar = element("progress", { attrs: { max: 100, value: Number.isFinite(progress.percent) ? progress.percent : 0 } });
    const progressText = Number.isFinite(progress.percent) ? `${progress.percent}%` : formatBytes(progress.bytes);
    progressRow.append(bar, element("span", { text: progressText }));
    body.append(progressRow);
  }
  if (item.file?.bytes) body.append(element("p", { className: "paper-meta", text: `${formatBytes(item.file.bytes)} · ${item.file.verificationLevel || "本地校验"}` }));
  if (item.cache?.bytes && !item.file?.bytes) body.append(element("p", { className: "paper-meta cache-proof", text: `${formatBytes(item.cache.bytes)} · 浏览器内部缓存 · %PDF 签名有效` }));
  if (item.identityApproval?.method === "user") {
    body.append(element("p", { className: "paper-meta manual-proof", text: "已人工查看并确认 · 归档时标记 manual-confirmed" }));
  }
  const zoteroMeta = zoteroStateMeta(item.zoteroSync);
  if (zoteroMeta) {
    body.append(element("span", {
      className: `zotero-state ${zoteroMeta[1]}`,
      text: zoteroMeta[0],
    }));
    if (item.zoteroSync.error) {
      body.append(element("p", { className: "paper-meta zotero-error", text: item.zoteroSync.error }));
    }
    const legacyLink = item.zoteroSync.state === "linked" && item.zoteroSync.attachmentMode !== "stored";
    if ((["pending", "failed"].includes(item.zoteroSync.state) || legacyLink) && item.state === ITEM_STATE.COMPLETED) {
      const zoteroActions = element("div", { className: "item-actions zotero-actions" });
      const retryZotero = element("button", { text: legacyLink ? "迁移到 Zotero 存储" : "同步到 Zotero", attrs: { type: "button" } });
      retryZotero.addEventListener("click", () => handlers.onRetryZotero(item.id));
      zoteroActions.append(retryZotero);
      body.append(zoteroActions);
    }
  }
  const captureListening = item.assistedCapture?.status === "listening";
  const captureEligible = !item.cache?.cacheKey
    && ![ITEM_STATE.COMPLETED, ITEM_STATE.ABANDONED, ITEM_STATE.CACHING, ITEM_STATE.ARCHIVING, ITEM_STATE.VERIFYING].includes(item.state);
  if (captureListening || captureEligible) {
    const captureActions = element("div", { className: "item-actions assisted-capture-actions" });
    const capture = element("button", {
      text: captureListening ? "停止捕获" : "捕获 PDF",
      attrs: { type: "button", class: captureListening ? "capture-listening" : "" },
    });
    capture.addEventListener("click", () => (captureListening
      ? handlers.onStopCapture(item.id)
      : handlers.onStartCapture(item.id)));
    captureActions.append(capture);
    if (captureListening) {
      captureActions.append(element("span", {
        className: "capture-hint",
        text: "正在监听当前标签页，请点击出版社 PDF / Download",
      }));
    }
    body.append(captureActions);
  }
  const reparseActions = element("div", { className: "item-actions" });
  const reparse = element("button", {
    text: item.doi ? "从 DOI 重新解析" : "重新解析文章页",
    attrs: { type: "button" },
  });
  reparse.addEventListener("click", () => handlers.onReparse(item.id));
  reparseActions.append(reparse);
  body.append(reparseActions);
  if ([ITEM_STATE.LOGIN_REQUIRED, ITEM_STATE.MANUAL_REQUIRED].includes(item.state)) {
    const actions = element("div", { className: "item-actions" });
    const open = element("button", { text: "打开验证页面", attrs: { type: "button" } });
    open.addEventListener("click", () => handlers.onOpen(item.id));
    const retry = element("button", {
      text: "验证完成，继续当前文献",
      attrs: { type: "button" },
    });
    retry.addEventListener("click", () => handlers.onRetry(item.id));
    actions.append(open, retry);
    body.append(actions);
  } else if ([ITEM_STATE.CANDIDATE_REGISTERED, ITEM_STATE.FAILED, ITEM_STATE.INVALID_RESPONSE, ITEM_STATE.QUARANTINED, ITEM_STATE.BLOCKED, ITEM_STATE.NO_ENTITLEMENT].includes(item.state)) {
    const actions = element("div", { className: "item-actions" });
    const retry = element("button", { text: "重新预检", attrs: { type: "button" } });
    retry.addEventListener("click", () => handlers.onRetry(item.id));
    actions.append(retry);
    const assetCandidate = item.candidates?.some((candidate) => candidate.sourceDetail === "sciencedirect-pdf-asset");
    if (item.state === ITEM_STATE.CANDIDATE_REGISTERED && assetCandidate) {
      const cache = element("button", { text: "缓存并验真", attrs: { type: "button" } });
      cache.addEventListener("click", () => handlers.onCache(item.id));
      actions.append(cache);
    }
    body.append(actions);
  }
  if (item.cache?.cacheKey && [ITEM_STATE.PDF_CACHED, ITEM_STATE.VERIFICATION_INCONCLUSIVE, ITEM_STATE.QUARANTINED].includes(item.state)) {
    const actions = element("div", { className: "item-actions cache-actions" });
    const view = element("button", { text: "查看缓存 PDF", attrs: { type: "button" } });
    view.addEventListener("click", () => handlers.onViewCache(item.id));
    actions.append(view);
    body.append(actions);
  }
  if (ABANDONABLE_STATES.has(item.state)) {
    const actions = element("div", { className: "item-actions" });
    const abandon = element("button", { text: "放弃此文献", attrs: { type: "button" } });
    abandon.addEventListener("click", () => handlers.onAbandon(item.id));
    actions.append(abandon);
    body.append(actions);
  }
  row.append(checkbox, body);
  return row;
}

export function renderPublisherGroups(container, items, selectedIds, handlers) {
  container.replaceChildren();
  if (!items.length) {
    container.append(element("div", { className: "empty-state", text: "当前任务没有文献" }));
    return;
  }
  for (const [publisherKey, papers] of groupByPublisher(items)) {
    const section = element("section", { className: "publisher-group" });
    const header = element("div", { className: "publisher-head" });
    header.append(element("strong", { text: papers[0]?.publisher || publisherKey }), element("span", { text: `${papers.length} 篇` }));
    const list = element("div", { className: "paper-items" });
    for (const paper of papers) list.append(renderPaper(paper, selectedIds.has(paper.id), handlers));
    section.append(header, list);
    container.append(section);
  }
}

export function renderDownloadSummary(container, items, selectedIds) {
  const selected = items.filter((item) => selectedIds.has(item.id));
  const cached = selected.filter((item) => item.state === ITEM_STATE.PDF_CACHED && item.cache?.cacheKey);
  container.replaceChildren(
    element("strong", { text: `${selected.length} 篇已选，${cached.length} 篇可归档` }),
    element("span", { text: "归档从浏览器内部缓存读取 PDF，经本地桥严格验真后直接写入目标目录，不经过 Chrome 下载目录。" }),
  );
}

export function renderCacheSummary(container, items, selectedIds) {
  const selected = items.filter((item) => selectedIds.has(item.id));
  const eligible = selected.filter((item) => (
    [ITEM_STATE.PDF_RESPONSE_VERIFIED, ITEM_STATE.AUTHORIZED, ITEM_STATE.QUEUED].includes(item.state)
      || (item.state === ITEM_STATE.CANDIDATE_REGISTERED
        && item.candidates?.some((candidate) => candidate.sourceDetail === "sciencedirect-pdf-asset"))
  ));
  const cached = selected.filter((item) => item.state === ITEM_STATE.PDF_CACHED);
  container.replaceChildren(
    element("strong", { text: `${selected.length} 篇已选，${eligible.length} 篇待缓存，${cached.length} 篇已缓存` }),
    element("span", { text: "扩展内部缓存复用当前机构会话且不产生浏览器下载；最终归档由本地桥执行严格验真。" }),
  );
}

export function renderPipelineOverview(container, items) {
  const total = items.length;
  const discovered = items.filter((item) => (item.candidates || []).length > 0
    || [ITEM_STATE.PDF_RESPONSE_VERIFIED, ITEM_STATE.CACHING, ITEM_STATE.PDF_CACHED, ITEM_STATE.ARCHIVING, ITEM_STATE.COMPLETED].includes(item.state)).length;
  const cached = items.filter((item) => [ITEM_STATE.PDF_CACHED, ITEM_STATE.ARCHIVING, ITEM_STATE.COMPLETED].includes(item.state)).length;
  const archived = items.filter((item) => item.state === ITEM_STATE.COMPLETED).length;
  container.replaceChildren();
  for (const [index, label, value] of [[1, "发现 PDF", discovered], [2, "浏览器缓存", cached], [3, "本地归档", archived]]) {
    const step = element("div", { className: "pipeline-step" });
    step.append(
      element("span", { className: "pipeline-index", text: String(index) }),
      element("strong", { text: label }),
      element("small", { text: `${value} / ${total}` }),
    );
    container.append(step);
  }
}

export function renderStorageMeter(container, storage, maxPdfBytes) {
  container.replaceChildren();
  if (!storage || !Number.isFinite(storage.quota) || storage.quota <= 0) {
    container.append(
      element("strong", { text: "浏览器磁盘缓存" }),
      element("span", { text: `单篇最多 ${formatBytes(maxPdfBytes)}；实际容量由 Chromium 与本机可用磁盘决定。` }),
    );
    return;
  }
  const safeAvailable = Math.max(0, Number(storage.available || 0) - 128 * 1024 * 1024);
  const estimatedPapers = Math.max(0, Math.floor(safeAvailable / Math.max(1, Number(maxPdfBytes || 1))));
  const progress = element("progress", {
    attrs: { max: 100, value: Number.isFinite(storage.percent) ? storage.percent : 0 },
  });
  container.append(
    element("strong", { text: `浏览器磁盘缓存 ${formatBytes(storage.usage)} / ${formatBytes(storage.quota)}` }),
    progress,
    element("span", { text: `当前可用 ${formatBytes(storage.available)}；按单篇上限估算还可缓存约 ${estimatedPapers} 篇。` }),
  );
}

export function renderCurrentAction(container, items, job = null) {
  const paused = job?.pausedItemId ? items.find((item) => item.id === job.pausedItemId) : null;
  const active = items.find((item) => [ITEM_STATE.CACHING, ITEM_STATE.ARCHIVING, ITEM_STATE.RESOLVING].includes(item.state));
  const manual = paused || items.find((item) => [ITEM_STATE.LOGIN_REQUIRED, ITEM_STATE.MANUAL_REQUIRED].includes(item.state));
  const item = active || manual;
  container.hidden = !item;
  container.replaceChildren();
  if (!item) return;
  const prefix = active ? "正在处理" : "需要你操作";
  const instruction = active
    ? item.statusReason || stateLabel(item.state)
    : item.publisherKey === "sciencedirect"
      ? "队列已停在这一篇。在文章页完成人工验证后点击“验证完成，继续当前文献”；扩展会点击 View PDF、捕获最终 PDF 并缓存，成功后自动进入下一篇。"
      : "队列已停在这一篇。在出版社页面完成登录或人工验证，再点击“验证完成，继续当前文献”；缓存成功后自动进入下一篇。";
  const pending = Array.isArray(job?.pendingDownloadItemIds) ? job.pendingDownloadItemIds.length : 0;
  container.append(
    element("span", { className: "eyebrow", text: prefix }),
    element("strong", { text: `${item.ordinal}. ${item.title}` }),
    element("p", { text: instruction }),
    ...(pending ? [element("small", { text: `当前文献完成后，队列中还剩 ${Math.max(0, pending - 1)} 篇。` })] : []),
  );
}

export function renderActivityLog(container, countElement, logs = []) {
  const recent = logs.slice(-120).reverse();
  countElement.textContent = `${logs.length} 条`;
  container.replaceChildren();
  if (!recent.length) {
    container.append(element("div", { className: "log-empty", text: "任务开始后，PDF 捕获、缓存、校验和归档事件会显示在这里。" }));
    return;
  }
  for (const log of recent) {
    const row = element("div", { className: `log-row ${log.level || "info"}` });
    const time = new Date(log.createdAt);
    row.append(
      element("time", { text: Number.isNaN(time.getTime()) ? "--:--:--" : time.toLocaleTimeString("zh-CN", { hour12: false }) }),
      element("span", { text: log.message || "任务状态已更新" }),
    );
    container.append(row);
  }
}
