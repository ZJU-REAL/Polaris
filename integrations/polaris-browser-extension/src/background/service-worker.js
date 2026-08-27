import { MESSAGE, ITEM_STATE, MAX_IMPORT_ITEMS, MINIMUM_NATIVE_BRIDGE_VERSION } from "../shared/constants.js";
import { validateYfrSelectionPayload } from "../shared/schema.js";
import { validatePolarisBatch, validatePolarisTask } from "../shared/polaris.js";
import { parseSafeHttpUrl, redactSensitiveUrl } from "../shared/url-security.js";
import { plannedPdfFilename } from "../shared/normalization.js";
import { createJob, getJobSnapshot, listJobs, updateJob } from "../jobs/job-service.js";
import { addLog, getAllRecords, getRecord, putRecord, putRecords, setSetting, getSetting } from "../storage/db.js";
import { sanitizeItemForPersistence } from "../storage/item-security.js";
import { canTransition, transitionCapturedPdfToCaching, transitionItem } from "../queue/state-machine.js";
import {
  queuePatchAfterAbandon,
  queuePatchAfterSettledItem,
  registrationItemsForQueue,
  registrationOutcomeAccepted,
  shouldPreserveRegistrationControl,
  statusAfterQueueEnds,
} from "../queue/job-control.js";
import { bestCandidate, registerMetadataCandidates, registerPublisherRuleCandidates, registerPublisherSnapshot } from "../publishers/registry.js";
import { expectedScienceDirectPii, registerScienceDirectPdfAsset, scienceDirectArticleIdentity } from "../publishers/sciencedirect-asset.js";
import { clearMetadataCache, resolvePaperMetadata } from "../publishers/metadata-resolver.js";
import { candidateCanUseBrowserCache, requiresPublisherNavigation } from "../queue/cache-policy.js";
import { closeTabQuietly, navigatePublisherPdf, preflightPublisherCandidateInTab, probePublisherTab, probePublisherTabById } from "./tab-probe.js";
import { browserPdfCacheKey, browserStorageEstimate, cachePdfBytesInBrowser, cachePdfInBrowser, copyBrowserCachedPdf, deleteBrowserCachedPdf, getBrowserCachedPdf } from "./browser-pdf-cache.js";
import { reusableCachedItems } from "./polaris-cache-reuse.js";
import { archiveCachedPdfWithBridge, invalidateNativeBridgeStatus, nativeBridgeStatus, sendNativeCommand } from "./native-bridge.js";
import { classifyPdfPreflight, preflightPdfCandidate, shouldRetireCandidateAfterPreflight } from "./pdf-preflight.js";
import { publicError, validateMessage } from "./messages.js";
import { handleActionClick } from "./action-router.js";
import { captureScienceDirectPdfAfterVerification, recoverScienceDirectPdfTab } from "./sciencedirect-recovery.js";
import { shouldAttemptScienceDirectCapture } from "./sciencedirect-session-policy.js";
import { hasPreferredBrowserNavigationCandidate } from "../publishers/pdf-rules.js";
import { planRegistrationSequence } from "../queue/registration-priority.js";
import { resetItemForDoiReparse } from "../queue/reparse.js";
import { runPolarisBatchWaves } from "../queue/polaris-batch.js";
import { clearPublisherRequestContextRules } from "./publisher-request-context.js";
import { startNavigationPdfCapture } from "./navigation-pdf-capture.js";
import {
  archivePdfToPolaris,
  createPolarisBatch,
  rebindLoopbackPolarisConnection,
  syncScnetSnapshots,
  testPolarisConnection,
} from "./polaris-client.js";
import { isReusableLocalArchive, runLocalFirstPolarisArchive } from "./local-first-archive.js";
import {
  bridgeInstallerState,
  openBridgeInstaller,
  showBridgeInstaller,
  startBridgeInstallerDownload,
} from "./bridge-installer.js";
import {
  disconnectZotero,
  pairWithZotero,
  setZoteroAutoSync,
  supportsStoredPdfSync,
  storedZoteroConnection,
  syncLinkedPdfToZotero,
  zoteroStatus,
} from "./zotero-client.js";

const runningRegistrations = new Map();
const runningDownloads = new Map();
const stopRequests = new Set();
const activeCacheControllers = new Map();
const activeNavigationCaptures = new Map();
const activeAssistedCaptures = new Map();
let pendingZoteroRetry = null;
let nativeArchiveTail = Promise.resolve();
const LAST_NATIVE_DESTINATION = "lastNativeDestination";
const POLARIS_CONNECTION = "polarisConnection";
const SCNET_CONTEXT = "scnetContext";

clearPublisherRequestContextRules(chrome).catch(() => undefined);

function registerCacheController(taskId, controller) {
  const controllers = activeCacheControllers.get(taskId) || new Set();
  controllers.add(controller);
  activeCacheControllers.set(taskId, controllers);
}

function unregisterCacheController(taskId, controller) {
  const controllers = activeCacheControllers.get(taskId);
  if (!controllers) return;
  controllers.delete(controller);
  if (!controllers.size) activeCacheControllers.delete(taskId);
}

async function runNativeArchiveExclusive(operation) {
  const previous = nativeArchiveTail;
  let release;
  nativeArchiveTail = new Promise((resolve) => { release = resolve; });
  await previous;
  try {
    return await operation();
  } finally {
    release();
  }
}

async function notifyStateChanged(taskId, reason) {
  try {
    await chrome.runtime.sendMessage({ type: MESSAGE.STATE_CHANGED, taskId, reason });
  } catch {
    // The side panel may be closed.
  }
}

async function persistItem(item) {
  const storedItem = sanitizeItemForPersistence(item);
  await putRecord("items", storedItem);
  await notifyStateChanged(storedItem.taskId, storedItem.state);
  return storedItem;
}

function zoteroSyncRecord(state, patch = {}) {
  return {
    state,
    itemKey: patch.itemKey || null,
    attachmentKey: patch.attachmentKey || null,
    attachmentMode: patch.attachmentMode || null,
    sourceRetained: patch.sourceRetained === true,
    collectionKey: patch.collectionKey || null,
    legacyLinkMigrated: patch.legacyLinkMigrated === true,
    error: patch.error || null,
    updatedAt: new Date().toISOString(),
  };
}

async function syncArchivedItemToZotero(job, original, { force = false } = {}) {
  if (original.state !== ITEM_STATE.COMPLETED || !original.file?.filename) return original;
  const [status, connection] = await Promise.all([
    zoteroStatus(chrome),
    storedZoteroConnection(chrome),
  ]);
  if (!force && connection.autoSync === false) {
    if (original.zoteroSync?.state) return original;
    return persistItem({
      ...original,
      zoteroSync: zoteroSyncRecord("pending", { error: "Zotero 自动同步已关闭" }),
    });
  }
  if (!status.available || !status.paired) {
    const reason = status.available ? "浏览器尚未与 Zotero 配对" : "Zotero 当前未运行";
    if (original.zoteroSync?.state === "pending" && original.zoteroSync?.error === reason) return original;
    return persistItem({
      ...original,
      zoteroSync: zoteroSyncRecord("pending", { error: reason }),
    });
  }

  let item = await persistItem({ ...original, zoteroSync: zoteroSyncRecord("syncing") });
  try {
    const result = await syncLinkedPdfToZotero(chrome, job, item);
    item = await persistItem({
      ...item,
      zoteroSync: zoteroSyncRecord("linked", {
        itemKey: result.itemKey,
        attachmentKey: result.attachmentKey,
        attachmentMode: result.attachmentMode,
        sourceRetained: result.sourceRetained,
        collectionKey: result.collectionKey,
        legacyLinkMigrated: result.legacyLinkMigrated,
      }),
    });
    await addLog(job.id, "success", result.attachmentMode === "stored"
      ? `第 ${item.ordinal} 篇已复制到 Zotero 存储并绑定条目`
      : `第 ${item.ordinal} 篇已关联到 Zotero 条目`);
    return item;
  } catch (error) {
    const retryable = Boolean(error?.retryable);
    const message = publicError(error, "Zotero PDF 关联失败");
    item = await persistItem({
      ...item,
      zoteroSync: zoteroSyncRecord(retryable ? "pending" : "failed", { error: message }),
    });
    await addLog(job.id, retryable ? "warn" : "error", `第 ${item.ordinal} 篇Zotero 同步${retryable ? "待重试" : "被拒绝"}：${message}`);
    return item;
  }
}

async function retryZoteroSyncs({ taskId = null, itemIds = null, includeFailed = false, force = false } = {}) {
  const selected = itemIds ? new Set(itemIds) : null;
  const [allItems, zotero] = await Promise.all([getAllRecords("items"), zoteroStatus(chrome)]);
  const canMigrateLegacyLinks = supportsStoredPdfSync(zotero.pluginVersion);
  const staleBefore = Date.now() - 60_000;
  const candidates = allItems.filter((item) => {
    if (item.state !== ITEM_STATE.COMPLETED || !item.file?.filename) return false;
    if (taskId && item.taskId !== taskId) return false;
    if (selected && !selected.has(item.id)) return false;
    const syncState = item.zoteroSync?.state || "pending";
    if (syncState === "pending") return true;
    if (syncState === "syncing") return new Date(item.zoteroSync.updatedAt || 0).getTime() < staleBefore;
    if (syncState === "linked") {
      return canMigrateLegacyLinks && item.zoteroSync?.attachmentMode !== "stored";
    }
    return includeFailed && syncState === "failed";
  }).sort((left, right) => String(left.taskId).localeCompare(String(right.taskId)) || left.ordinal - right.ordinal);
  let linked = 0;
  for (const item of candidates) {
    const job = await getRecord("jobs", item.taskId);
    if (!job) continue;
    const result = await syncArchivedItemToZotero(job, item, { force });
    if (result.zoteroSync?.state === "linked") linked += 1;
  }
  return { ok: true, processed: candidates.length, linked };
}

function schedulePendingZoteroRetry() {
  if (pendingZoteroRetry) return pendingZoteroRetry;
  pendingZoteroRetry = retryZoteroSyncs().catch(() => ({ ok: false })).finally(() => {
    pendingZoteroRetry = null;
  });
  return pendingZoteroRetry;
}

function absoluteCandidateUrl(candidateUrl, sourceUrl) {
  if (!candidateUrl) return null;
  try {
    const resolved = new URL(candidateUrl, sourceUrl || undefined).toString();
    return parseSafeHttpUrl(resolved, { allowLocalDevelopment: true });
  } catch {
    return null;
  }
}

async function currentState(taskId = null) {
  const jobs = await listJobs();
  const selectedTaskId = taskId || await getSetting("currentTaskId", jobs[0]?.id || null);
  const snapshot = selectedTaskId ? await getJobSnapshot(selectedTaskId) : null;
  const installerDownloadId = await getSetting("bridgeInstallerDownloadId", null);
  const installerVersion = await getSetting("bridgeInstallerVersion", null);
  const currentInstallerId = installerVersion === MINIMUM_NATIVE_BRIDGE_VERSION ? installerDownloadId : null;
  const [bridge, bridgeInstaller, storage, zotero, storedPolarisConnection, storedScnetContext] = await Promise.all([
    nativeBridgeStatus(chrome),
    bridgeInstallerState(chrome, currentInstallerId),
    browserStorageEstimate(),
    zoteroStatus(chrome),
    getSetting(POLARIS_CONNECTION, null),
    getSetting(SCNET_CONTEXT, null),
  ]);
  if (zotero.available && zotero.paired && zotero.autoSync) void schedulePendingZoteroRetry();
  return {
    jobs,
    currentTaskId: selectedTaskId,
    snapshot,
    bridge,
    bridgeInstaller,
    storage,
    zotero,
    polarisConnection: storedPolarisConnection ? {
      instanceOrigin: storedPolarisConnection.instanceOrigin,
      user: storedPolarisConnection.user,
      updatedAt: storedPolarisConnection.updatedAt,
    } : null,
    scnetContext: storedScnetContext ? {
      credentialId: storedScnetContext.credentialId,
      instanceOrigin: storedScnetContext.instanceOrigin,
      updatedAt: storedScnetContext.updatedAt,
    } : null,
  };
}

async function setCurrentTask(taskId) {
  await setSetting("currentTaskId", taskId);
  await notifyStateChanged(taskId, "current-task");
}

async function optionsWithRememberedDestination(options = {}) {
  if (options.destinationId || options.destinationMode === "browser-downloads") return options;
  const remembered = await getSetting(LAST_NATIVE_DESTINATION, null);
  if (!remembered?.destinationId || !remembered?.displayPath) return options;
  return {
    ...options,
    destinationMode: "native-bridge",
    destinationId: remembered.destinationId,
    displayPath: remembered.displayPath,
  };
}

async function importYfrSelection(message, sender) {
  const senderUrl = sender.tab?.url || sender.url;
  if (!senderUrl) throw new Error("无法确认 YFR 页面来源");
  let senderOrigin;
  try { senderOrigin = new URL(senderUrl).origin; } catch { throw new Error("YFR 页面来源无效"); }
  if (senderOrigin !== message.pageOrigin) throw new Error("YFR 页面来源校验失败");
  const validated = validateYfrSelectionPayload(message.payload, message.pageOrigin);
  if (!validated.ok) throw new Error(validated.error);
  const { source, papers, selectedCount, paperIds } = validated.value;
  const origin = { ...source, selectedCount, paperIds };
  const jobOptions = await optionsWithRememberedDestination();
  const built = await createJob(origin, papers, {
    ...jobOptions,
    expectedCount: selectedCount,
    expectedPaperIds: paperIds,
  });
  await setCurrentTask(built.job.id);
  await addLog(built.job.id, "info", `已从 ${source.area} 导入 ${built.items.length} 篇文献，编号与勾选数量校验通过`);
  if (sender.tab?.windowId != null) {
    try { await chrome.sidePanel.open({ windowId: sender.tab.windowId }); } catch { /* Chrome may not preserve user activation. */ }
  }
  return { ok: true, taskId: built.job.id, message: `已加入 Polaris 扩展：${built.items.length} 篇` };
}

async function importPolarisTask(message, sender) {
  const senderUrl = sender.tab?.url || sender.url;
  if (!senderUrl) throw new Error("无法确认 Polaris 页面来源");
  let senderOrigin;
  try { senderOrigin = new URL(senderUrl).origin; } catch { throw new Error("Polaris 页面来源无效"); }
  if (senderOrigin !== message.pageOrigin) throw new Error("Polaris 页面来源校验失败");
  const validated = validatePolarisTask(message.payload, message.pageOrigin);
  if (!validated.ok) throw new Error(validated.error);
  const nonceKey = `polarisNonce:${validated.value.paper.polarisTarget.nonce}`;
  if (await getSetting(nonceKey, null)) throw new Error("该 Polaris 下载任务已接收");
  const built = await createJob(validated.value.source, [validated.value.paper], {
    ...(await optionsWithRememberedDestination()),
    expectedCount: 1,
  });
  const incoming = built.items[0];
  const cachedCandidates = reusableCachedItems(incoming, await getAllRecords("items"));
  for (const cached of cachedCandidates) {
    const copied = await copyBrowserCachedPdf({
      sourceKey: cached.cache.cacheKey,
      taskId: built.job.id,
      itemId: incoming.id,
    });
    if (!copied) continue;
    const reused = await putRecord("items", {
      ...incoming,
      state: ITEM_STATE.PDF_CACHED,
      statusReason: "已复用同一论文的浏览器缓存，跳过重复下载；当前 Polaris 归档目标保持独立绑定",
      cache: {
        ...cached.cache,
        ...copied,
        bytes: copied.bytes || Number(cached.cache.bytes || 0),
        cachedAt: new Date().toISOString(),
        reusedFromItemId: cached.id,
      },
      updatedAt: new Date().toISOString(),
    });
    built.items[0] = reused;
    built.job = await updateJob(built.job.id, { status: "awaiting-archive" });
    await addLog(built.job.id, "success", "已复用验真 PDF 缓存；未重复下载，归档仍绑定当前 Polaris 论文");
    break;
  }
  await setSetting(nonceKey, new Date().toISOString());
  await setCurrentTask(built.job.id);
  await addLog(built.job.id, "info", "已从 Polaris 论文库接收下载任务");
  if (sender.tab?.windowId != null) {
    try { await chrome.sidePanel.open({ windowId: sender.tab.windowId }); } catch { /* User activation may expire. */ }
  }
  return {
    ok: true,
    taskId: built.job.id,
    reusedCache: built.items[0].state === ITEM_STATE.PDF_CACHED,
    message: built.items[0].state === ITEM_STATE.PDF_CACHED
      ? "已复用缓存并加入 Polaris 归档队列"
      : "已加入 Polaris 扩展",
  };
}

async function reuseCachesForPolarisBatch(built) {
  const existingItems = await getAllRecords("items");
  let reusedCount = 0;
  for (let index = 0; index < built.items.length; index += 1) {
    const incoming = built.items[index];
    const cachedCandidates = reusableCachedItems(incoming, existingItems);
    for (const cached of cachedCandidates) {
      const copied = await copyBrowserCachedPdf({
        sourceKey: cached.cache.cacheKey,
        taskId: built.job.id,
        itemId: incoming.id,
      });
      if (!copied) continue;
      const reused = await putRecord("items", {
        ...incoming,
        state: ITEM_STATE.PDF_CACHED,
        statusReason: "已复用同一论文的浏览器缓存，跳过重复下载；当前 Polaris 归档目标保持独立绑定",
        cache: {
          ...cached.cache,
          ...copied,
          bytes: copied.bytes || Number(cached.cache.bytes || 0),
          cachedAt: new Date().toISOString(),
          reusedFromItemId: cached.id,
        },
        updatedAt: new Date().toISOString(),
      });
      built.items[index] = reused;
      reusedCount += 1;
      await addLog(built.job.id, "success", `第 ${reused.ordinal} 篇已复用验真 PDF 缓存，独立 Polaris 绑定保持不变`);
      break;
    }
  }
  if (reusedCount === built.items.length) {
    built.job = await updateJob(built.job.id, { status: "awaiting-archive" });
  }
  return reusedCount;
}

async function importPolarisBatch(message, sender) {
  const senderUrl = sender.tab?.url || sender.url;
  if (!senderUrl) throw new Error("无法确认 Polaris 页面来源");
  let senderOrigin;
  try { senderOrigin = new URL(senderUrl).origin; } catch { throw new Error("Polaris 页面来源无效"); }
  if (senderOrigin !== message.pageOrigin) throw new Error("Polaris 页面来源校验失败");
  const validated = validatePolarisBatch(message.payload, message.pageOrigin);
  if (!validated.ok) throw new Error(validated.error);
  const batchNonceKey = `polarisBatchNonce:${validated.value.batchNonce}`;
  if (await getSetting(batchNonceKey, null)) throw new Error("该 Polaris 批量任务已接收");
  const built = await createJob(validated.value.source, validated.value.papers, {
    ...(await optionsWithRememberedDestination()),
    expectedCount: validated.value.papers.length,
  });
  const connection = await getSetting(POLARIS_CONNECTION, null);
  let backendBatchId = validated.value.backendBatchId;
  if (!backendBatchId && connection?.instanceOrigin && connection?.apiKey) {
    const backendBatch = await createPolarisBatch({
      instanceOrigin: connection.instanceOrigin,
      apiKey: connection.apiKey,
      papers: built.items,
    });
    backendBatchId = backendBatch.id;
  }
  if (backendBatchId) {
    built.job = await updateJob(built.job.id, {
      origin: { ...built.job.origin, backendBatchId },
    });
    for (let index = 0; index < built.items.length; index += 1) {
      built.items[index] = {
        ...built.items[index],
        polarisTarget: { ...built.items[index].polarisTarget, backendBatchId },
      };
      await putRecord("items", sanitizeItemForPersistence(built.items[index]));
    }
  }
  const reusedCount = await reuseCachesForPolarisBatch(built);
  await setSetting(batchNonceKey, new Date().toISOString());
  await setCurrentTask(built.job.id);
  await addLog(built.job.id, "info", `已从 Polaris 接收 1 个批量任务，共 ${built.items.length} 篇论文`);
  if (sender.tab?.windowId != null) {
    try { await chrome.sidePanel.open({ windowId: sender.tab.windowId }); } catch { /* User activation may expire. */ }
  }
  return {
    ok: true,
    taskId: built.job.id,
    count: built.items.length,
    reusedCacheCount: reusedCount,
    message: `已加入 1 个 Polaris 下载任务，共 ${built.items.length} 篇论文`,
  };
}

async function importRecords(message) {
  if (!Array.isArray(message.records) || message.records.length < 1 || message.records.length > MAX_IMPORT_ITEMS) {
    throw new Error(`导入数量必须为 1-${MAX_IMPORT_ITEMS}`);
  }
  const source = message.source && typeof message.source === "object" ? message.source : { type: "doi-list" };
  const built = await createJob(source, message.records, await optionsWithRememberedDestination(message.options || {}));
  await setCurrentTask(built.job.id);
  await addLog(built.job.id, "info", `已导入 ${built.items.length} 篇去重文献`);
  return { ok: true, taskId: built.job.id, count: built.items.length };
}

function startResolving(item) {
  if (item.state === ITEM_STATE.RESOLVING) return item;
  const retryingResponse = [ITEM_STATE.INVALID_RESPONSE, ITEM_STATE.FAILED].includes(item.state);
  return transitionItem(item, ITEM_STATE.RESOLVING, retryingResponse ? { failedCandidateUrls: [] } : {});
}

async function preflightRegisteredCandidate(job, item, publisherTabId = null) {
  const candidate = bestCandidate(item);
  const url = absoluteCandidateUrl(candidate?.url, job.origin?.sourceUrl);
  if (!candidate || !url) return item;
  let result = null;
  let transport = "extension";
  if (typeof publisherTabId === "number") {
    try {
      const tabResult = await preflightPublisherCandidateInTab(chrome, publisherTabId, url);
      result = classifyPdfPreflight({
        status: tabResult.status,
          contentType: tabResult.contentType,
          bytes: new Uint8Array(Array.isArray(tabResult.bytes) ? tabResult.bytes : []),
          finalUrl: tabResult.finalUrl || url,
      });
      transport = "publisher-tab";
    } catch {
      // Cross-origin candidates and unsupported publisher pages fall back to the extension request.
    }
  }
  if (!result && requiresPublisherNavigation(candidate)) {
    return {
      ...item,
      state: ITEM_STATE.MANUAL_REQUIRED,
      statusReason: "该 PDF 入口依赖当前出版社标签页会话，请在原标签页完成验证后重新解析",
      updatedAt: new Date().toISOString(),
    };
  }
  if (!result) result = await preflightPdfCandidate(url);
  const checkedAt = new Date().toISOString();
  const candidates = (item.candidates || []).map((entry) => entry.url === candidate.url
    ? {
        ...entry,
        preflight: {
          status: result.kind,
          mime: result.mime || null,
          signatureMatched: Boolean(result.signatureMatched),
          transport,
          checkedAt,
        },
      }
    : entry);
  if (result.kind === "verified") {
    return {
      ...item,
      candidates,
      state: ITEM_STATE.PDF_RESPONSE_VERIFIED,
      statusReason: result.reason,
      activeCandidateUrl: url,
      updatedAt: checkedAt,
    };
  }
  if (["not-pdf", "access-required"].includes(result.kind)) {
    const retireCandidate = shouldRetireCandidateAfterPreflight(candidate, item.publisherKey, result);
    const failedCandidateUrls = retireCandidate
      ? Array.from(new Set([...(item.failedCandidateUrls || []), candidate.url]))
      : item.failedCandidateUrls;
    const hasFallbackCandidate = retireCandidate && (item.candidates || []).some((entry) => (
      entry.url !== candidate.url && !new Set(failedCandidateUrls || []).has(entry.url)
    ));
    const scienceDirectHint = item.publisherKey === "sciencedirect"
      ? "ScienceDirect View PDF 返回了验证页面，请打开文章页完成验证后重检"
      : result.reason;
    return {
      ...item,
      candidates,
      failedCandidateUrls,
      state: hasFallbackCandidate ? ITEM_STATE.CANDIDATE_REGISTERED : ITEM_STATE.MANUAL_REQUIRED,
      statusReason: hasFallbackCandidate ? `${result.reason}；已切换到下一个候选入口` : scienceDirectHint,
      updatedAt: checkedAt,
    };
  }
  return {
    ...item,
    candidates,
    state: ITEM_STATE.CANDIDATE_REGISTERED,
    statusReason: result.reason,
    updatedAt: checkedAt,
  };
}

async function preflightCandidateChain(job, item, publisherTabId = null) {
  let current = item;
  const limit = Math.max(1, (item.candidates || []).length);
  for (let attempt = 0; attempt < limit; attempt += 1) {
    const before = bestCandidate(current);
    if (!before) {
      return (current.candidates || []).length
        ? { ...current, state: ITEM_STATE.MANUAL_REQUIRED, statusReason: "全部已登记 PDF 候选均未通过校验，请人工检查文章页或放弃该文献" }
        : current;
    }
    const checked = await preflightRegisteredCandidate(job, current, publisherTabId);
    if (checked.state === ITEM_STATE.PDF_RESPONSE_VERIFIED) return checked;
    const after = bestCandidate(checked);
    if (checked.state !== ITEM_STATE.CANDIDATE_REGISTERED || !after || after.url === before.url) return checked;
    current = checked;
  }
  return current;
}

async function cacheCapturedNavigationPdf(job, item, match, navigationPdf) {
  const result = await cachePdfBytesInBrowser({
    taskId: job.id,
    itemId: item.id,
    bytes: navigationPdf.bytes,
    maxBytes: job.limits.maxPdfBytes,
    sourceMime: navigationPdf.mime,
  });
  const candidate = registerScienceDirectPdfAsset(item, match);
  const caching = transitionCapturedPdfToCaching(candidate, {
    cacheProgress: { bytes: result.bytes, totalBytes: result.bytes, percent: 100 },
    statusReason: "已读取浏览器成功导航响应，正在校验 PDF 文件签名",
  });
  const verifying = transitionItem(caching, ITEM_STATE.VERIFYING, {
    statusReason: "浏览器导航响应已缓存，正在复核 PDF 文件签名",
  });
  const cache = {
    storage: "extension-cache",
    cacheName: result.cacheName,
    cacheKey: result.cacheKey,
    bytes: result.bytes,
    mime: result.mime,
    sourceMime: result.sourceMime,
    signatureMatched: true,
    verificationLevel: "browser-navigation-signature",
    cachedAt: new Date().toISOString(),
  };
  return persistItem(transitionItem(verifying, ITEM_STATE.PDF_CACHED, {
    statusReason: "PDF 已从浏览器导航响应写入缓存并通过文件签名校验",
    cache,
    activeCandidateUrl: null,
    candidates: (candidate.candidates || []).map((entry) => entry.sessionBound
      ? { ...entry, url: redactSensitiveUrl(entry.url), signedUrlCleared: true }
      : entry),
  }));
}

async function cacheCapturedPublisherPdf(job, item, candidate, navigationPdf) {
  const result = await cachePdfBytesInBrowser({
    taskId: job.id,
    itemId: item.id,
    bytes: navigationPdf.bytes,
    maxBytes: job.limits.maxPdfBytes,
    sourceMime: navigationPdf.mime,
  });
  const checkedAt = new Date().toISOString();
  const candidates = (item.candidates || []).map((entry) => entry.url === candidate.url
    ? {
        ...entry,
        preflight: {
          status: "verified",
          mime: navigationPdf.mime || "application/pdf",
          signatureMatched: true,
          transport: "browser-navigation",
          checkedAt,
        },
      }
    : entry);
  const caching = transitionCapturedPdfToCaching({ ...item, candidates }, {
    activeCandidateUrl: candidate.url,
    cacheProgress: { bytes: result.bytes, totalBytes: result.bytes, percent: 100 },
    statusReason: "已读取当前浏览器会话的 PDF 导航响应",
  });
  const verifying = transitionItem(caching, ITEM_STATE.VERIFYING, {
    statusReason: "浏览器导航响应已缓存，正在复核 PDF 文件签名",
  });
  const cache = {
    storage: "extension-cache",
    cacheName: result.cacheName,
    cacheKey: result.cacheKey,
    bytes: result.bytes,
    mime: result.mime,
    sourceMime: result.sourceMime,
    signatureMatched: true,
    verificationLevel: "browser-navigation-signature",
    cachedAt: checkedAt,
  };
  return persistItem(transitionItem(verifying, ITEM_STATE.PDF_CACHED, {
    statusReason: "PDF 已从当前浏览器会话写入缓存并通过文件签名校验",
    cache,
    activeCandidateUrl: null,
  }));
}

function prepareAssistedCaptureItem(item, candidate) {
  let current = {
    ...item,
    candidates: Array.from(new Map([...(item.candidates || []), candidate]
      .map((entry) => [`${entry.url}\n${entry.sourceDetail || entry.source || ""}`, entry])).values()),
  };
  if (current.state !== ITEM_STATE.CANDIDATE_REGISTERED) {
    if (current.state !== ITEM_STATE.RESOLVING) {
      if (!canTransition(current.state, ITEM_STATE.RESOLVING)) {
        throw new Error("当前文献正在缓存、归档或已经完成，不能启动人工 PDF 捕获");
      }
      current = transitionItem(current, ITEM_STATE.RESOLVING);
    }
    current = transitionItem(current, ITEM_STATE.CANDIDATE_REGISTERED);
  }
  return current;
}

async function finalizeAssistedPdfCapture(taskId, itemId, sessionId, capture, pageUrl, previousStatusReason) {
  try {
    const navigationPdf = await capture.waitForPdf();
    const snapshot = await getJobSnapshot(taskId);
    const latest = snapshot?.items.find((entry) => entry.id === itemId);
    if (!latest || latest.assistedCapture?.sessionId !== sessionId) return;
    const candidate = {
      url: pageUrl,
      source: "publisher-page",
      sourceDetail: "assisted-page-capture",
      kind: "institutional",
      sessionBound: true,
      browserNavigationPreferred: true,
      allowUnboundDocument: true,
      allowUnboundPdfResponse: true,
      navigationTimeoutMs: 120000,
      discoveredAt: new Date().toISOString(),
      articleUrl: latest.articleUrl || pageUrl,
      retriableAfterAccess: true,
    };
    const prepared = prepareAssistedCaptureItem(latest, candidate);
    const cached = await cacheCapturedPublisherPdf(snapshot.job, prepared, candidate, navigationPdf);
    await persistItem({
      ...cached,
      assistedCapture: null,
      assistedCaptureResult: {
        status: "captured",
        capturedAt: new Date().toISOString(),
        sourceUrl: redactSensitiveUrl(navigationPdf.sourceUrl),
      },
    });
    await addLog(taskId, "success", `第 ${latest.ordinal} 篇已捕获并验真 PDF：${navigationPdf.bytes.byteLength} 字节`);
  } catch (error) {
    const snapshot = await getJobSnapshot(taskId);
    const latest = snapshot?.items.find((entry) => entry.id === itemId);
    if (latest?.assistedCapture?.sessionId === sessionId) {
      await persistItem({
        ...latest,
        assistedCapture: null,
        assistedCaptureResult: {
          status: "not-captured",
          finishedAt: new Date().toISOString(),
          reason: publicError(error, "未捕获到验真 PDF"),
        },
        statusReason: previousStatusReason || latest.statusReason,
        updatedAt: new Date().toISOString(),
      });
      await addLog(taskId, "warn", `第 ${latest.ordinal} 篇${publicError(error, "未捕获到验真 PDF")}`);
    }
  } finally {
    const active = activeAssistedCaptures.get(taskId);
    if (active?.sessionId === sessionId) activeAssistedCaptures.delete(taskId);
    await capture.close();
  }
}

async function startAssistedPdfCapture(taskId, itemId) {
  const snapshot = await getJobSnapshot(taskId);
  const item = snapshot?.items.find((entry) => entry.id === itemId);
  if (!snapshot || !item) throw new Error("文献任务不存在");
  if (item.cache?.cacheKey || [ITEM_STATE.PDF_CACHED, ITEM_STATE.COMPLETED].includes(item.state)) {
    throw new Error("该文献已经存在验真 PDF，无需再次捕获");
  }
  if ([ITEM_STATE.CACHING, ITEM_STATE.ARCHIVING, ITEM_STATE.VERIFYING].includes(item.state)) {
    throw new Error("该文献正在处理，请等待当前操作完成");
  }
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  const pageUrl = parseSafeHttpUrl(tab?.url || tab?.pendingUrl);
  if (typeof tab?.id !== "number" || !pageUrl) throw new Error("请先在当前窗口打开该论文的出版社文章页");

  const automatic = activeNavigationCaptures.get(taskId);
  if (automatic) {
    throw new Error("该任务正在自动捕获 PDF，请等待当前步骤结束后再启动人工捕获");
  }
  const previous = activeAssistedCaptures.get(taskId);
  if (previous) {
    await previous.capture.close(new Error("已开始捕获另一篇文献"));
    await previous.completion;
  }

  const sessionId = `${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
  let downloadEventLogged = false;
  const capture = await startNavigationPdfCapture(chrome, {
    initialTabId: tab.id,
    maxBytes: snapshot.job.limits.maxPdfBytes,
    timeoutMs: 120000,
    allowAnyHttpPdf: true,
    allowUnboundDocument: true,
    allowUnboundPdfResponse: true,
    failOnAccessDenied: false,
    onDownloadCandidate: ({ sourceUrl }) => {
      if (downloadEventLogged) return;
      downloadEventLogged = true;
      void addLog(taskId, "info", `第 ${item.ordinal} 篇已观察到浏览器 PDF 下载事件，正在读取并校验文件签名：${redactSensitiveUrl(sourceUrl)}`);
    },
  });
  const expiresAt = new Date(Date.now() + 120000).toISOString();
  await persistItem({
    ...item,
    assistedCapture: { status: "listening", sessionId, tabId: tab.id, startedAt: new Date().toISOString(), expiresAt },
    statusReason: "正在监听当前标签页；请在 120 秒内点击出版社 PDF 或 Download 按钮",
    updatedAt: new Date().toISOString(),
  });
  const entry = { capture, itemId, sessionId, completion: null };
  activeAssistedCaptures.set(taskId, entry);
  entry.completion = finalizeAssistedPdfCapture(taskId, itemId, sessionId, capture, pageUrl, item.statusReason);
  await addLog(taskId, "info", `第 ${item.ordinal} 篇开始监听当前标签页的 PDF 响应，最长 120 秒`);
  return { ok: true, expiresAt };
}

async function stopAssistedPdfCapture(taskId, itemId) {
  const active = activeAssistedCaptures.get(taskId);
  if (!active || active.itemId !== itemId) throw new Error("该文献当前没有正在运行的 PDF 捕获");
  await active.capture.close(new Error("PDF 捕获已由用户停止"));
  await active.completion;
  return { ok: true };
}

async function capturePublisherCandidateIntoCache(job, item, tabId, candidate) {
  let navigationCapture = null;
  try {
    await addLog(job.id, "info", `第 ${item.ordinal} 篇正在使用当前浏览器会话获取 ${candidate.sourceDetail || "出版社 PDF"}`);
    navigationCapture = await startNavigationPdfCapture(chrome, {
      initialTabId: tabId,
      expectedUrl: candidate.url,
      maxBytes: job.limits.maxPdfBytes,
      timeoutMs: Number(candidate.navigationTimeoutMs) || 20000,
      allowAnyHttpPdf: true,
      allowUnboundDocument: Boolean(candidate.actionId || candidate.allowUnboundDocument),
      allowUnboundPdfResponse: Boolean(candidate.allowUnboundPdfResponse),
      failOnAccessDenied: !candidate.waitThroughAccessChallenge,
      expectedQueryParams: candidate.expectedQueryParams || null,
    });
    activeNavigationCaptures.set(job.id, navigationCapture);
    await navigatePublisherPdf(chrome, tabId, candidate);
    const navigationPdf = await navigationCapture.waitForPdf();
    const cached = await cacheCapturedPublisherPdf(job, item, candidate, navigationPdf);
    await addLog(job.id, "success", `第 ${item.ordinal} 篇已通过浏览器会话捕获 PDF 并完成缓存：${navigationPdf.bytes.byteLength} 字节`);
    return cached;
  } finally {
    if (activeNavigationCaptures.get(job.id) === navigationCapture) activeNavigationCaptures.delete(job.id);
    await navigationCapture?.close();
  }
}

async function captureScienceDirectIntoCache(job, original, articleTabId) {
  let navigationCapture = null;
  try {
    await addLog(job.id, "info", `第 ${original.ordinal} 篇正在直接尝试 ScienceDirect View PDF`);
    navigationCapture = await startNavigationPdfCapture(chrome, {
      initialTabId: articleTabId,
      expectedPii: expectedScienceDirectPii(original),
      maxBytes: job.limits.maxPdfBytes,
    });
    activeNavigationCaptures.set(job.id, navigationCapture);
    const match = await captureScienceDirectPdfAfterVerification(chrome, {
      ...original,
      articleTabId,
      manualTabId: articleTabId,
    });
    await navigationCapture.ensureTab(match.tab.id);
    const navigationPdf = await navigationCapture.waitForPdf();
    const cached = await cacheCapturedNavigationPdf(job, original, match, navigationPdf);
    await addLog(job.id, "success", `第 ${original.ordinal} 篇已直接捕获 ScienceDirect PDF 并完成缓存：${navigationPdf.bytes.byteLength} 字节`);
    return cached;
  } finally {
    if (activeNavigationCaptures.get(job.id) === navigationCapture) activeNavigationCaptures.delete(job.id);
    await navigationCapture?.close();
  }
}

async function scienceDirectManualFallback(job, item, articleTabId, error, activate = true) {
  await deleteBrowserCachedPdf(browserPdfCacheKey(job.id, item.id));
  if (activate) {
    try { await chrome.tabs.update(articleTabId, { active: true }); } catch { /* The user may have closed the tab. */ }
  }
  return persistItem({
    ...item,
    state: ITEM_STATE.MANUAL_REQUIRED,
    manualTabId: articleTabId,
    articleTabId,
    statusReason: publicError(error, "ScienceDirect 直接获取 PDF 失败，请在当前页面完成验证后重检"),
    updatedAt: new Date().toISOString(),
  });
}

async function registerOne(job, original, { deferManual = false } = {}) {
  let item = await persistItem(startResolving(original));
  const knownScienceDirectTabId = original.publisherKey === "sciencedirect"
    ? original.articleTabId || original.manualTabId
    : null;
  if (original.publisherKey === "sciencedirect") {
    await addLog(job.id, "info", `第 ${original.ordinal} 篇正在检查当前 ScienceDirect 会话是否可直接获取 PDF`);
    if (typeof knownScienceDirectTabId === "number") {
      try {
        return await captureScienceDirectIntoCache(job, item, knownScienceDirectTabId);
      } catch (error) {
        return scienceDirectManualFallback(job, item, knownScienceDirectTabId, error, !deferManual);
      }
    }
    const recovered = await recoverScienceDirectPdfTab(chrome, item);
    if (recovered && typeof recovered.manualTabId === "number") {
      try {
        return await captureScienceDirectIntoCache(job, recovered, recovered.manualTabId);
      } catch (error) {
        return scienceDirectManualFallback(job, recovered, recovered.manualTabId, error, !deferManual);
      }
    }
  }
  const metadata = await resolvePaperMetadata(item);
  item = registerMetadataCandidates(item, metadata);
  item = registerPublisherRuleCandidates(item, item.articleUrl);
  const hasOpenCandidate = item.candidates.some((candidate) => candidate.kind === "open-access");
  const pageUrl = item.articleUrl || (item.doi ? `https://doi.org/${item.doi}` : null);
  if (hasOpenCandidate) {
    const openItem = {
      ...item,
      state: ITEM_STATE.CANDIDATE_REGISTERED,
      statusReason: "已发现开放 PDF 入口，将在缓存写入过程中校验文件签名",
    };
    if (candidateCanUseBrowserCache(openItem, bestCandidate(openItem)) || !pageUrl) return persistItem(openItem);
    item = openItem;
  }
  if (!pageUrl) {
    return persistItem({ ...item, state: ITEM_STATE.MANUAL_REQUIRED, statusReason: "缺少 DOI 和出版社文章页" });
  }

  let result = null;
  if (item.publisherKey !== "sciencedirect" && typeof item.manualTabId === "number") {
    try {
      const [tab, snapshot] = await Promise.all([
        chrome.tabs.get(item.manualTabId),
        probePublisherTabById(chrome, item.manualTabId),
      ]);
      result = { tabId: item.manualTabId, tab, snapshot };
    } catch {
      // Closed or non-scriptable manual tabs are recreated below.
    }
  }
  if (!result) {
    result = await probePublisherTab(chrome, pageUrl, {
      active: false,
      acceptPartialSnapshot: hasPreferredBrowserNavigationCandidate(item),
    });
  }
  if (result.error) {
    const scienceDirectIdentity = scienceDirectArticleIdentity(result.tab?.pendingUrl || result.tab?.url)
      || scienceDirectArticleIdentity(item.articleUrl);
    if (scienceDirectIdentity) {
      const scienceDirectItem = {
        ...item,
        adapter: "sciencedirect",
        publisherKey: "sciencedirect",
        articleUrl: scienceDirectIdentity.articleUrl,
        articleTabId: result.tabId,
        identifiers: { ...(item.identifiers || {}), pii: scienceDirectIdentity.pii },
        statusReason: "已识别 Elsevier 跳转页，正在自动尝试 View PDF",
      };
      try {
        return await captureScienceDirectIntoCache(job, scienceDirectItem, result.tabId);
      } catch (error) {
        return scienceDirectManualFallback(job, scienceDirectItem, result.tabId, error, !deferManual);
      }
    }
    const redirectedPageUrl = result.tab?.pendingUrl || result.tab?.url || pageUrl;
    const redirectedItem = registerPublisherRuleCandidates({
      ...item,
      articleUrl: redirectedPageUrl,
    }, redirectedPageUrl);
    const redirectedCandidate = bestCandidate(redirectedItem);
    if (redirectedCandidate?.browserNavigationPreferred
      && (redirectedCandidate.directReader || redirectedCandidate.source === "publisher-rule")) {
      try {
        const captured = await capturePublisherCandidateIntoCache(job, redirectedItem, result.tabId, redirectedCandidate);
        await closeTabQuietly(chrome, result.tabId);
        return captured;
      } catch (error) {
        item = {
          ...redirectedItem,
          state: ITEM_STATE.MANUAL_REQUIRED,
          manualTabId: result.tabId,
          statusReason: publicError(error, "PDF reader requires verification in the current publisher tab"),
          updatedAt: new Date().toISOString(),
        };
        if (!deferManual) {
          try { await chrome.tabs.update(result.tabId, { active: true }); } catch { /* tab may be gone */ }
        }
        return persistItem({ ...item, articleUrl: pageUrl });
      }
    }
    try { await chrome.tabs.update(result.tabId, { active: true }); } catch { /* tab may be gone */ }
    return persistItem({
      ...item,
      state: ITEM_STATE.MANUAL_REQUIRED,
      manualTabId: result.tabId,
      articleUrl: pageUrl,
      statusReason: publicError(result.error, "出版社页面需要人工检查"),
    });
  }
  item = registerPublisherSnapshot(item, result.snapshot);
  if (shouldAttemptScienceDirectCapture(item, result.tabId)) {
    try {
      return await captureScienceDirectIntoCache(job, { ...item, articleTabId: result.tabId }, result.tabId);
    } catch (error) {
      return scienceDirectManualFallback(job, item, result.tabId, error, !deferManual);
    }
  }
  let browserCandidate = bestCandidate(item);
  if (browserCandidate?.browserNavigationPreferred) {
    try {
      const captured = await capturePublisherCandidateIntoCache(job, item, result.tabId, browserCandidate);
      await closeTabQuietly(chrome, result.tabId);
      return captured;
    } catch (error) {
      item = {
        ...item,
        state: ITEM_STATE.MANUAL_REQUIRED,
        manualTabId: result.tabId,
        statusReason: publicError(error, "当前浏览器会话尚未取得 PDF，请在已打开页面完成验证后重新解析"),
        updatedAt: new Date().toISOString(),
      };
    }
  }
  if (![ITEM_STATE.LOGIN_REQUIRED, ITEM_STATE.MANUAL_REQUIRED, ITEM_STATE.NO_ENTITLEMENT, ITEM_STATE.PDF_CACHED].includes(item.state)) {
    item = await preflightCandidateChain(job, item, result.tabId);
  }
  const manual = [ITEM_STATE.LOGIN_REQUIRED, ITEM_STATE.MANUAL_REQUIRED].includes(item.state);
  if (manual) {
    item.manualTabId = result.tabId;
    if (item.publisherKey === "sciencedirect") item.articleTabId = result.tabId;
    if (!deferManual) {
      try { await chrome.tabs.update(result.tabId, { active: true }); } catch { /* tab may be gone */ }
    }
  } else {
    await closeTabQuietly(chrome, result.tabId);
  }
  return persistItem(item);
}

async function finishStoppedTask(taskId) {
  const snapshot = await getJobSnapshot(taskId);
  if (!snapshot) throw new Error("下载任务不存在");
  const settled = new Set([ITEM_STATE.PDF_CACHED, ITEM_STATE.COMPLETED, ITEM_STATE.ABANDONED]);
  const existingPending = Array.isArray(snapshot.job.pendingDownloadItemIds)
    ? snapshot.job.pendingDownloadItemIds.filter((id) => {
        const item = snapshot.items.find((entry) => entry.id === id);
        return item && !settled.has(item.state);
      })
    : [];
  const pendingDownloadItemIds = existingPending.length
    ? existingPending
    : snapshot.items.filter((item) => !settled.has(item.state)).map((item) => item.id);
  await updateJob(taskId, {
    status: "stopped",
    pausedItemId: snapshot.job.pausedItemId || pendingDownloadItemIds[0] || null,
    pendingDownloadItemIds,
  });
  await addLog(taskId, "warn", "任务已由用户停止");
  await notifyStateChanged(taskId, "stopped");
  stopRequests.delete(taskId);
  return { ok: true, stopped: true };
}

async function resumeStoppedTask(taskId) {
  const snapshot = await getJobSnapshot(taskId);
  if (!snapshot) throw new Error("下载任务不存在");
  if (!["stopped", "paused"].includes(snapshot.job.status)) throw new Error("当前任务不处于停止状态");
  const settled = new Set([ITEM_STATE.PDF_CACHED, ITEM_STATE.COMPLETED, ITEM_STATE.ABANDONED]);
  const saved = new Set(snapshot.job.pendingDownloadItemIds || []);
  const pending = snapshot.items
    .filter((item) => !settled.has(item.state) && (!saved.size || saved.has(item.id)))
    .sort((left, right) => left.ordinal - right.ordinal)
    .map((item) => item.id);
  if (!pending.length) throw new Error("当前任务没有可继续处理的文献");
  stopRequests.delete(taskId);
  await updateJob(taskId, {
    status: "queued",
    pausedItemId: null,
    pendingDownloadItemIds: pending,
  });
  await addLog(taskId, "info", `继续处理剩余 ${pending.length} 篇文献`);
  if (snapshot.job.queueMode === "archive") return runDownloads(taskId, pending);
  if (snapshot.job.queueMode === "cache") return runCaching(taskId, pending);
  const selected = new Set(pending);
  return runRegistration(taskId, (item) => selected.has(item.id), true, pending[0]);
}

async function requestTaskStop(taskId) {
  const snapshot = await getJobSnapshot(taskId);
  if (!snapshot) throw new Error("下载任务不存在");
  stopRequests.add(taskId);
  await updateJob(taskId, { status: "stopping" });
  for (const controller of activeCacheControllers.get(taskId) || []) controller.abort();
  await activeNavigationCaptures.get(taskId)?.close(new Error("PDF 捕获已由用户停止"));
  await activeAssistedCaptures.get(taskId)?.capture.close(new Error("PDF 捕获已由用户停止"));
  await notifyStateChanged(taskId, "stopping");
  if (!runningRegistrations.has(taskId) && !runningDownloads.has(taskId)) await finishStoppedTask(taskId);
  return { ok: true };
}

async function runRegistration(taskId, itemFilter = null, preserveControl = false, priorityItemId = null) {
  const activeRegistration = runningRegistrations.get(taskId);
  if (activeRegistration) {
    await activeRegistration;
    return itemFilter ? runRegistration(taskId, itemFilter, preserveControl, priorityItemId) : { ok: true };
  }
  stopRequests.delete(taskId);
  const task = (async () => {
    const snapshot = await getJobSnapshot(taskId);
    if (!snapshot) throw new Error("下载任务不存在");
    if (runningDownloads.has(taskId) && snapshot.job.status === "downloading") {
      throw new Error("当前下载尚未完成，请等待队列暂停或结束后再预检");
    }
    const preserveJobControl = shouldPreserveRegistrationControl(snapshot.job, preserveControl);
    const managesSequentialQueue = !preserveControl || snapshot.job.queueMode === "registration";
    const scopedItems = itemFilter ? snapshot.items.filter(itemFilter) : snapshot.items;
    let items = registrationItemsForQueue(snapshot.job, snapshot.items, scopedItems, managesSequentialQueue && preserveControl);
    items = items.map((item) => registerPublisherRuleCandidates(item, item.articleUrl));
    items = planRegistrationSequence(items, priorityItemId);
    if (!items.length) throw new Error("当前没有尚待处理的选中文献");
    if (managesSequentialQueue) {
      await updateJob(taskId, {
        status: "registering",
        queueMode: "registration",
        queueItemIds: preserveControl && Array.isArray(snapshot.job.queueItemIds)
          ? snapshot.job.queueItemIds
          : items.map((item) => item.id),
        pausedItemId: null,
        pendingDownloadItemIds: items.map((item) => item.id),
      });
    } else if (!preserveJobControl) {
      await updateJob(taskId, { status: "registering" });
    }
    await addLog(taskId, "info", managesSequentialQueue
      ? `开始逐篇发现、验证并缓存 ${items.length} 篇文献`
      : "开始复检文献 PDF 响应");
    let pausedItem = null;
    for (let index = 0; index < items.length; index += 1) {
      if (stopRequests.has(taskId)) break;
      const planned = items[index];
      const remainingIds = items.slice(index + 1).map((item) => item.id);
      const item = await getRecord("items", planned.id);
      if (!item || item.taskId !== taskId) {
        if (managesSequentialQueue) await updateJob(taskId, { pendingDownloadItemIds: remainingIds });
        continue;
      }
      if ([ITEM_STATE.COMPLETED, ITEM_STATE.ABANDONED, ITEM_STATE.PDF_CACHED].includes(item.state)) {
        if (managesSequentialQueue) await updateJob(taskId, { pendingDownloadItemIds: remainingIds });
        continue;
      }
      try {
        let outcome = await registerOne(snapshot.job, item, { deferManual: !managesSequentialQueue });
        if (stopRequests.has(taskId)) break;
        if (candidateCanUseBrowserCache(outcome, bestCandidate(outcome))) {
          outcome = await cacheOne(snapshot.job, outcome);
          while (outcome.state === ITEM_STATE.INVALID_RESPONSE
            && candidateCanUseBrowserCache(outcome, bestCandidate(outcome))) {
            await addLog(taskId, "warn", `第 ${outcome.ordinal} 篇开放入口返回非 PDF，正在尝试下一个可信候选`);
            outcome = await cacheOne(snapshot.job, outcome);
          }
        }
        if (managesSequentialQueue && !registrationOutcomeAccepted(outcome)) {
          pausedItem = outcome;
          await updateJob(taskId, {
            status: "awaiting-user",
            queueMode: "registration",
            pausedItemId: outcome.id,
            pendingDownloadItemIds: [outcome.id, ...remainingIds],
          });
          await addLog(taskId, "warn", `流程暂停在第 ${outcome.ordinal} 篇；取得并缓存 PDF 或明确放弃后才会进入下一篇`);
          break;
        }
        if (managesSequentialQueue) {
          await updateJob(taskId, { pendingDownloadItemIds: remainingIds });
        }
      } catch (error) {
        if (stopRequests.has(taskId)) break;
        const failed = await persistItem({ ...item, state: ITEM_STATE.FAILED, retryCount: Number(item.retryCount || 0) + 1, statusReason: publicError(error) });
        if (managesSequentialQueue) {
          pausedItem = failed;
          await updateJob(taskId, {
            status: "awaiting-user",
            queueMode: "registration",
            pausedItemId: failed.id,
            pendingDownloadItemIds: [failed.id, ...remainingIds],
          });
          await addLog(taskId, "error", `流程暂停在第 ${failed.ordinal} 篇；重试或放弃后才会继续`);
          break;
        }
      }
    }
    if (stopRequests.has(taskId)) return finishStoppedTask(taskId);
    if (managesSequentialQueue && pausedItem) {
      const manualTabId = pausedItem.articleTabId || pausedItem.manualTabId;
      if (typeof manualTabId === "number") {
        try { await chrome.tabs.update(manualTabId, { active: true }); } catch { /* The page can be recreated on demand. */ }
      }
      await addLog(taskId, "warn", `等待处理第 ${pausedItem.ordinal} 篇文献`);
    } else if (managesSequentialQueue) {
      const finalSnapshot = await getJobSnapshot(taskId);
      await updateJob(taskId, {
        status: statusAfterQueueEnds(finalSnapshot.items),
        queueMode: null,
        pausedItemId: null,
        pendingDownloadItemIds: [],
      });
    }
    if (!pausedItem) await addLog(taskId, "info", managesSequentialQueue
      ? "所选文献已按顺序完成处理；可用 PDF 均已缓存并验真"
      : "文献 PDF 响应复检完成");
    await notifyStateChanged(taskId, "registration-complete");
    return { ok: true };
  })().finally(() => runningRegistrations.delete(taskId));
  runningRegistrations.set(taskId, task);
  return task;
}

async function cacheOne(job, original) {
  if (original.publisherKey === "sciencedirect") {
    throw new Error("ScienceDirect 必须通过浏览器导航响应捕获，禁止对临时签名地址发起二次请求");
  }
  const sourceUrl = job.origin?.sourceUrl;
  const candidate = bestCandidate(original);
  const url = absoluteCandidateUrl(candidate?.url, sourceUrl);
  if (!candidate || !url) throw new Error("没有可下载的安全 PDF 候选地址");
  if (requiresPublisherNavigation(candidate)) {
    throw new Error("该 PDF 入口必须复用原出版社标签页会话，禁止后台二次请求");
  }
  if (!candidateCanUseBrowserCache(original, candidate)) throw new Error("PDF 入口尚未达到可缓存条件");
  let item = await persistItem(transitionItem(original, ITEM_STATE.CACHING, {
    activeCandidateUrl: url,
    cacheProgress: { bytes: 0, totalBytes: 0, percent: 0 },
    statusReason: "正在写入扩展内部 PDF 缓存",
  }));
  const controller = new AbortController();
  registerCacheController(job.id, controller);
  try {
    await addLog(job.id, "info", `第 ${item.ordinal} 篇开始写入浏览器内部缓存`);
    const singleCacheAttempt = () => cachePdfInBrowser({
      taskId: job.id,
      itemId: item.id,
      url,
      maxBytes: job.limits.maxPdfBytes,
      signal: controller.signal,
      onProgress: async (progress) => {
        item = await persistItem({
          ...item,
          cacheProgress: progress,
          statusReason: progress.percent == null
            ? `正在缓存 PDF：${Math.round(progress.bytes / 1024)} KiB`
            : `正在缓存 PDF：${progress.percent}%`,
          updatedAt: new Date().toISOString(),
        });
      },
    });
    const result = await singleCacheAttempt();
    item = await persistItem(transitionItem(item, ITEM_STATE.VERIFYING, {
      cacheProgress: { bytes: result.bytes, totalBytes: result.bytes, percent: 100 },
      statusReason: "缓存写入完成，正在复核 PDF 文件签名",
    }));
    const cache = {
      storage: "extension-cache",
      cacheName: result.cacheName,
      cacheKey: result.cacheKey,
      bytes: result.bytes,
      mime: result.mime,
      sourceMime: result.sourceMime,
      signatureMatched: true,
      verificationLevel: "browser-signature",
      cachedAt: new Date().toISOString(),
    };
    const cached = await persistItem(transitionItem(item, ITEM_STATE.PDF_CACHED, {
      statusReason: "PDF 已写入浏览器内部缓存并通过文件签名校验",
      cache,
      activeCandidateUrl: null,
      candidates: (item.candidates || []).map((entry) => entry.sessionBound
        ? { ...entry, url: redactSensitiveUrl(entry.url), signedUrlCleared: true }
        : entry),
    }));
    await addLog(job.id, "success", `第 ${item.ordinal} 篇缓存完成：${result.bytes} 字节，PDF 签名有效`);
    return cached;
  } catch (error) {
    await deleteBrowserCachedPdf(browserPdfCacheKey(job.id, original.id));
    const stopped = stopRequests.has(job.id);
    const rawDetail = publicError(error, "PDF 缓存失败");
    const detail = rawDetail;
    const invalid = /HTML|非 PDF|PDF 响应|文件签名|文件过小/.test(detail);
    await addLog(job.id, stopped ? "warn" : "error", `第 ${item.ordinal} 篇${stopped ? "缓存已停止" : detail}`);
    return persistItem(transitionItem(item, stopped ? ITEM_STATE.BLOCKED : invalid ? ITEM_STATE.INVALID_RESPONSE : ITEM_STATE.FAILED, {
      retryCount: Number(item.retryCount || 0) + 1,
      statusReason: stopped ? "缓存已由用户停止" : detail,
      failedCandidateUrls: invalid ? Array.from(new Set([...(item.failedCandidateUrls || []), url])) : item.failedCandidateUrls,
    }));
  } finally {
    unregisterCacheController(job.id, controller);
  }
}

async function runCaching(taskId, selectedItemIds = null) {
  if (runningDownloads.has(taskId)) return runningDownloads.get(taskId);
  if (runningRegistrations.has(taskId)) throw new Error("所选文献正在预检并自动缓存，请等待或停止当前任务");
  stopRequests.delete(taskId);
  const task = (async () => {
    const snapshot = await getJobSnapshot(taskId);
    if (!snapshot) throw new Error("下载任务不存在");
    const selected = selectedItemIds ? new Set(selectedItemIds) : null;
    const terminalStates = [ITEM_STATE.PDF_CACHED, ITEM_STATE.COMPLETED, ITEM_STATE.ABANDONED];
    const sequence = snapshot.items.filter((item) => {
      if (terminalStates.includes(item.state)) return false;
      if (selected && !selected.has(item.id)) return false;
      return candidateCanUseBrowserCache(item, bestCandidate(item));
    }).sort((left, right) => left.ordinal - right.ordinal);
    if (!sequence.length) throw new Error("当前没有尚待缓存的已选文献");
    await updateJob(taskId, {
      status: "caching",
      queueMode: "cache",
      pausedItemId: null,
      pendingDownloadItemIds: sequence.map((item) => item.id),
    });
    await addLog(taskId, "info", `开始逐篇缓存并验真 ${sequence.length} 篇文献`);

    const acceptedStates = new Set([ITEM_STATE.PDF_CACHED]);
    let pausedItem = null;
    if (snapshot.job.origin?.type === "polaris") {
      const batch = await runPolarisBatchWaves(sequence, async (planned) => {
        const current = await getRecord("items", planned.id);
        if (!current || current.taskId !== taskId || terminalStates.includes(current.state)) return current;
        if (!candidateCanUseBrowserCache(current, bestCandidate(current))) return current;
        let outcome = await cacheOne(snapshot.job, current);
        while (outcome.state === ITEM_STATE.INVALID_RESPONSE && bestCandidate(outcome)) {
          await addLog(taskId, "warn", `第 ${outcome.ordinal} 篇返回非 PDF，先预检下一个候选入口`);
          outcome = await persistItem(await preflightCandidateChain(snapshot.job, outcome));
          if (!candidateCanUseBrowserCache(outcome, bestCandidate(outcome))) break;
          outcome = await cacheOne(snapshot.job, outcome);
        }
        return outcome;
      }, {
        maxConcurrency: 2,
        shouldStop: () => stopRequests.has(taskId),
        onWaveComplete: async (remaining) => updateJob(taskId, {
          pendingDownloadItemIds: remaining.map((item) => item.id),
        }),
      });
      pausedItem = batch.results.find((item) => item && !acceptedStates.has(item.state)) || null;
      if (pausedItem) {
        const pendingIds = batch.results
          .filter((item) => item && !acceptedStates.has(item.state))
          .map((item) => item.id);
        await updateJob(taskId, {
          status: "awaiting-user",
          pausedItemId: pausedItem.id,
          pendingDownloadItemIds: [...pendingIds, ...batch.remaining.map((item) => item.id)],
        });
        await addLog(taskId, "warn", `批量缓存完成，可处理论文已继续；${pendingIds.length + batch.remaining.length} 篇仍需处理`);
      }
    } else for (let index = 0; index < sequence.length; index += 1) {
      if (stopRequests.has(taskId)) break;
      const planned = sequence[index];
      const remainingIds = sequence.slice(index + 1).map((item) => item.id);
      const current = await getRecord("items", planned.id);
      if (!current || current.taskId !== taskId) {
        await updateJob(taskId, { pendingDownloadItemIds: remainingIds });
        continue;
      }
      if (terminalStates.includes(current.state)) {
        await updateJob(taskId, { pendingDownloadItemIds: remainingIds });
        continue;
      }
      if (!candidateCanUseBrowserCache(current, bestCandidate(current))) {
        pausedItem = current;
        await updateJob(taskId, {
          status: "awaiting-user",
          pausedItemId: current.id,
          pendingDownloadItemIds: [current.id, ...remainingIds],
        });
        await addLog(taskId, "warn", `缓存队列暂停在第 ${current.ordinal} 篇；请完成预检、人工验证或放弃该文献`);
        break;
      }
      let outcome = await cacheOne(snapshot.job, current);
      while (outcome.state === ITEM_STATE.INVALID_RESPONSE && bestCandidate(outcome)) {
        await addLog(taskId, "warn", `第 ${outcome.ordinal} 篇返回非 PDF，先预检下一个候选入口`);
        outcome = await persistItem(await preflightCandidateChain(snapshot.job, outcome));
        if (!candidateCanUseBrowserCache(outcome, bestCandidate(outcome))) break;
        outcome = await cacheOne(snapshot.job, outcome);
      }
      if (!acceptedStates.has(outcome.state)) {
        pausedItem = outcome;
        await updateJob(taskId, {
          status: "awaiting-user",
          pausedItemId: outcome.id,
          pendingDownloadItemIds: [outcome.id, ...remainingIds],
        });
        await addLog(taskId, "warn", `缓存队列暂停在第 ${outcome.ordinal} 篇；完成验证、重试或放弃后继续`);
        break;
      }
      await updateJob(taskId, { pendingDownloadItemIds: remainingIds });
    }
    const finalSnapshot = await getJobSnapshot(taskId);
    if (stopRequests.has(taskId)) return finishStoppedTask(taskId);
    const cached = finalSnapshot.items.filter((item) => item.state === ITEM_STATE.PDF_CACHED).length;
    const unresolved = finalSnapshot.items.filter((item) => ![ITEM_STATE.PDF_CACHED, ITEM_STATE.COMPLETED, ITEM_STATE.NO_ENTITLEMENT, ITEM_STATE.ABANDONED].includes(item.state)).length;
    if (!pausedItem) {
      await updateJob(taskId, {
        status: cached ? "awaiting-archive" : "partial",
        pausedItemId: null,
        pendingDownloadItemIds: [],
      });
      await addLog(taskId, "info", `缓存流程结束：${cached} 篇已缓存并验真，${unresolved} 篇待处理`);
    }
    return { ok: true, cached, unresolved, pausedItemId: pausedItem?.id || null };
  })().finally(() => runningDownloads.delete(taskId));
  runningDownloads.set(taskId, task);
  return task;
}

async function archiveCachedOne(job, original) {
  if (!original.cache?.cacheKey) throw new Error("浏览器内部缓存不存在，请重新缓存该文献");
  const response = await getBrowserCachedPdf(original.cache.cacheKey);
  if (!response) throw new Error("浏览器内部缓存已被清理，请重新缓存该文献");
  let item = await persistItem(transitionItem(original, ITEM_STATE.ARCHIVING, {
    archiveProgress: { bytes: 0, totalBytes: Number(original.cache.bytes || 0), percent: 0 },
    statusReason: "正在把浏览器缓存流式归档到目标目录",
  }));
  try {
    await addLog(job.id, "info", `第 ${item.ordinal} 篇开始从浏览器缓存归档`);
    if (item.polarisTarget) {
      const connection = await getSetting(POLARIS_CONNECTION, null);
      const archived = await archivePdfToPolaris({ response, item, connection });
      await deleteBrowserCachedPdf(item.cache.cacheKey);
      const completed = await persistItem(transitionItem(item, ITEM_STATE.COMPLETED, {
        statusReason: "PDF 已归档到 Polaris 论文库",
        file: { storage: "polaris", ...archived },
        cache: { ...item.cache, archivedAt: new Date().toISOString() },
      }));
      await addLog(job.id, "success", `第 ${item.ordinal} 篇已归档到 Polaris`);
      return completed;
    }
    const bridgeResult = await archiveCachedPdfWithBridge(chrome, response, {
      destinationId: job.destination.destinationId,
      taskCode: job.taskCode,
      fileName: item.plannedFilename,
      itemId: item.id,
      expectedDoi: item.doi,
      expectedTitle: item.title,
      manualApproval: item.identityApproval?.method === "user",
      maxBytes: job.limits.maxPdfBytes,
      metadata: {
        ordinal: item.ordinal,
        title: item.title,
        doi: item.doi,
        publisher: item.publisher,
        articleUrl: item.articleUrl,
      },
      onProgress: async ({ bytes }) => {
        if (stopRequests.has(job.id)) throw new Error("归档已由用户停止");
        const totalBytes = Number(item.cache?.bytes || 0);
        item = await persistItem({
          ...item,
          archiveProgress: {
            bytes,
            totalBytes,
            percent: totalBytes ? Math.min(99, Math.floor((bytes / totalBytes) * 100)) : null,
          },
          statusReason: totalBytes ? `正在归档 PDF：${Math.min(99, Math.floor((bytes / totalBytes) * 100))}%` : `正在归档 PDF：${Math.round(bytes / 1024)} KiB`,
          updatedAt: new Date().toISOString(),
        });
      },
    });
    if (["rejected", "invalid"].includes(bridgeResult.status)) {
      await deleteBrowserCachedPdf(item.cache.cacheKey);
      await addLog(job.id, "error", `第 ${item.ordinal} 篇缓存未通过本地桥 PDF 复验`);
      return persistItem(transitionItem(item, ITEM_STATE.INVALID_RESPONSE, { statusReason: bridgeResult.message || "缓存 PDF 在归档前复验失败" }));
    }
    const identityVerification = {
      decisionBasis: bridgeResult.decisionBasis || null,
      detectedDoi: bridgeResult.detectedDoi || null,
      detectedDois: Array.isArray(bridgeResult.detectedDois) ? bridgeResult.detectedDois.slice(0, 32) : [],
      doiMatched: Boolean(bridgeResult.doiMatched),
      titleSimilarity: Number(bridgeResult.titleSimilarity || 0),
      checkedAt: new Date().toISOString(),
    };
    if (bridgeResult.status === "mismatch") {
      await addLog(job.id, "warn", `第 ${item.ordinal} 篇身份证据存在冲突，浏览器缓存已保留供人工核验`);
      return persistItem(transitionItem(item, ITEM_STATE.QUARANTINED, {
        statusReason: `${bridgeResult.message} 浏览器缓存仍保留，可查看后人工确认。`,
        identityVerification,
      }));
    }
    if (bridgeResult.status === "quarantined") {
      await deleteBrowserCachedPdf(item.cache.cacheKey);
      await addLog(job.id, "warn", `第 ${item.ordinal} 篇已归档到隔离目录`);
      return persistItem(transitionItem(item, ITEM_STATE.QUARANTINED, { statusReason: bridgeResult.message, file: bridgeResult.file, identityVerification, cache: { ...item.cache, archivedAt: new Date().toISOString() } }));
    }
    if (bridgeResult.status === "inconclusive") {
      await addLog(job.id, "warn", `第 ${item.ordinal} 篇身份待确认，浏览器缓存已保留供人工核验`);
      return persistItem(transitionItem(item, ITEM_STATE.VERIFICATION_INCONCLUSIVE, {
        statusReason: `${bridgeResult.message} 浏览器缓存仍保留，可查看后人工确认。`,
        identityVerification,
      }));
    }
    await deleteBrowserCachedPdf(item.cache.cacheKey);
    let completed = await persistItem(transitionItem(item, ITEM_STATE.COMPLETED, {
      statusReason: bridgeResult.message || "PDF 已通过本地桥复验并归档",
      file: bridgeResult.file,
      identityVerification,
      cache: { ...item.cache, archivedAt: new Date().toISOString() },
      zoteroSync: zoteroSyncRecord("pending"),
    }));
    await addLog(job.id, "success", item.identityApproval?.method === "user"
      ? `第 ${item.ordinal} 篇已按人工确认结果归档完成`
      : `第 ${item.ordinal} 篇已通过严格验真并归档完成`);
    completed = await syncArchivedItemToZotero(job, completed);
    return completed;
  } catch (error) {
    await addLog(job.id, stopRequests.has(job.id) ? "warn" : "error", `第 ${item.ordinal} 篇${publicError(error, "PDF 归档失败")}`);
    return persistItem(transitionItem(item, ITEM_STATE.PDF_CACHED, {
      statusReason: `${publicError(error, "PDF 归档失败")}；缓存文件仍保留，可重试归档`,
    }));
  }
}

async function archivePolarisCachedOneLocalFirst(job, original) {
  if (!original.cache?.cacheKey) throw new Error("浏览器内部缓存不存在，请重新缓存该文献");
  const localResponse = await getBrowserCachedPdf(original.cache.cacheKey);
  if (!localResponse) throw new Error("浏览器内部缓存已被清理，请重新缓存该文献");
  let item = await persistItem(transitionItem(original, ITEM_STATE.ARCHIVING, {
    archiveProgress: { bytes: 0, totalBytes: Number(original.cache.bytes || 0), percent: 0 },
    statusReason: isReusableLocalArchive(original.localArchive)
      ? "本地文件已保存，正在重试同步到 Polaris"
      : "正在通过下载桥保存到本地固定目录",
  }));
  const identityFromBridge = (result) => ({
    decisionBasis: result?.decisionBasis || null,
    detectedDoi: result?.detectedDoi || null,
    detectedDois: Array.isArray(result?.detectedDois) ? result.detectedDois.slice(0, 32) : [],
    doiMatched: Boolean(result?.doiMatched),
    titleSimilarity: Number(result?.titleSimilarity || 0),
    checkedAt: new Date().toISOString(),
  });
  try {
    await addLog(job.id, "info", isReusableLocalArchive(item.localArchive)
      ? `第 ${item.ordinal} 篇复用已保存的本地文件，重试同步 Polaris`
      : `第 ${item.ordinal} 篇开始通过下载桥保存到固定目录`);
    const outcome = await runLocalFirstPolarisArchive({
      localArchive: item.localArchive,
      saveLocal: () => runNativeArchiveExclusive(() => archiveCachedPdfWithBridge(chrome, localResponse, {
        destinationId: job.destination.destinationId,
        taskCode: job.taskCode,
        fileName: item.plannedFilename,
        itemId: item.id,
        expectedDoi: item.doi,
        expectedTitle: item.title,
        manualApproval: item.identityApproval?.method === "user",
        maxBytes: job.limits.maxPdfBytes,
        metadata: {
          ordinal: item.ordinal,
          title: item.title,
          doi: item.doi,
          publisher: item.publisher,
          articleUrl: item.articleUrl,
          polarisTarget: item.polarisTarget,
        },
        onProgress: async ({ bytes }) => {
          if (stopRequests.has(job.id)) throw new Error("归档已由用户停止");
          const totalBytes = Number(item.cache?.bytes || 0);
          item = await persistItem({
            ...item,
            archiveProgress: {
              bytes,
              totalBytes,
              percent: totalBytes ? Math.min(99, Math.floor((bytes / totalBytes) * 100)) : null,
            },
            statusReason: totalBytes
              ? `正在保存到本地固定目录：${Math.min(99, Math.floor((bytes / totalBytes) * 100))}%`
              : `正在保存到本地固定目录：${Math.round(bytes / 1024)} KiB`,
            updatedAt: new Date().toISOString(),
          });
        },
      })),
      persistLocal: async (localArchive, bridgeResult) => {
        item = await persistItem({
          ...item,
          localArchive,
          identityVerification: identityFromBridge(bridgeResult),
          archiveProgress: {
            bytes: Number(item.cache?.bytes || 0),
            totalBytes: Number(item.cache?.bytes || 0),
            percent: 100,
          },
          statusReason: "PDF 已保存到本地固定目录，正在同步到 Polaris",
          updatedAt: new Date().toISOString(),
        });
        await addLog(job.id, "success", `第 ${item.ordinal} 篇已保存到本地固定目录，开始同步 Polaris`);
      },
      uploadCloud: async () => {
        const cloudResponse = await getBrowserCachedPdf(item.cache.cacheKey);
        if (!cloudResponse) throw new Error("本地文件已保存，但浏览器缓存不可用，无法同步 Polaris");
        const connection = await getSetting(POLARIS_CONNECTION, null);
        return archivePdfToPolaris({ response: cloudResponse, item, connection });
      },
    });
    if (!outcome.completed) {
      const bridgeResult = outcome.bridgeResult || {};
      const identityVerification = identityFromBridge(bridgeResult);
      if (["rejected", "invalid"].includes(bridgeResult.status)) {
        await deleteBrowserCachedPdf(item.cache.cacheKey);
        await addLog(job.id, "error", `第 ${item.ordinal} 篇未通过本地下载桥 PDF 复验`);
        return persistItem(transitionItem(item, ITEM_STATE.INVALID_RESPONSE, {
          statusReason: bridgeResult.message || "缓存 PDF 在本地保存前复验失败",
        }));
      }
      if (bridgeResult.status === "mismatch") {
        await addLog(job.id, "warn", `第 ${item.ordinal} 篇身份校验冲突，未同步 Polaris`);
        return persistItem(transitionItem(item, ITEM_STATE.QUARANTINED, {
          statusReason: `${bridgeResult.message || "PDF 身份不匹配"}；浏览器缓存仍保留，可人工确认`,
          identityVerification,
        }));
      }
      if (bridgeResult.status === "quarantined") {
        await deleteBrowserCachedPdf(item.cache.cacheKey);
        return persistItem(transitionItem(item, ITEM_STATE.QUARANTINED, {
          statusReason: bridgeResult.message || "PDF 已保存到本地隔离目录，未同步 Polaris",
          file: bridgeResult.file,
          identityVerification,
          cache: { ...item.cache, archivedAt: new Date().toISOString() },
        }));
      }
      return persistItem(transitionItem(item, ITEM_STATE.VERIFICATION_INCONCLUSIVE, {
        statusReason: `${bridgeResult.message || "PDF 身份证据不足"}；浏览器缓存仍保留，可人工确认`,
        identityVerification,
      }));
    }
    await deleteBrowserCachedPdf(item.cache.cacheKey);
    const completed = await persistItem(transitionItem(item, ITEM_STATE.COMPLETED, {
      statusReason: "PDF 已保存到本地固定目录并归档到 Polaris 论文库",
      file: { storage: "polaris", ...outcome.cloudArchive, local: outcome.localArchive.file },
      localArchive: outcome.localArchive,
      cache: { ...item.cache, archivedAt: new Date().toISOString() },
    }));
    await addLog(job.id, "success", `第 ${item.ordinal} 篇已完成本地保存与 Polaris 归档`);
    return completed;
  } catch (error) {
    const localSaved = isReusableLocalArchive(item.localArchive);
    await addLog(job.id, stopRequests.has(job.id) ? "warn" : "error", `第 ${item.ordinal} 篇${publicError(error, "PDF 归档失败")}`);
    return persistItem(transitionItem(item, ITEM_STATE.PDF_CACHED, {
      statusReason: localSaved
        ? `${publicError(error, "Polaris 云端同步失败")}；本地文件与浏览器缓存均已保留，可重试云端同步`
        : `${publicError(error, "本地保存失败")}；浏览器缓存仍保留，可重试归档`,
    }));
  }
}

async function runDownloads(taskId, selectedItemIds = null) {
  if (runningDownloads.has(taskId)) return runningDownloads.get(taskId);
  if (runningRegistrations.has(taskId)) throw new Error("文献预检仍在运行，请等待或停止当前任务");
  stopRequests.delete(taskId);
  const task = (async () => {
    const snapshot = await getJobSnapshot(taskId);
    if (!snapshot) throw new Error("下载任务不存在");
    const bridge = await nativeBridgeStatus(chrome);
    if (!bridge.connected || !bridge.compatible) throw new Error("归档 PDF 前请安装或更新本地下载桥");
    if (snapshot.job.destination.mode !== "native-bridge" || !snapshot.job.destination.destinationId) {
      const remembered = await getSetting(LAST_NATIVE_DESTINATION, null);
      const preferred = remembered?.destinationId ? remembered : bridge.defaultDestination;
      if (!preferred?.destinationId || !preferred?.displayPath) throw new Error("请先选择 PDF 归档目录");
      snapshot.job = await updateJob(taskId, {
        destination: {
          ...snapshot.job.destination,
          mode: "native-bridge",
          destinationId: preferred.destinationId,
          displayPath: preferred.displayPath,
        },
      });
    }
    const selected = selectedItemIds ? new Set(selectedItemIds) : null;
    const sequence = snapshot.items.filter((item) => item.state === ITEM_STATE.PDF_CACHED && (!selected || selected.has(item.id)));
    if (!sequence.length) throw new Error("当前没有已缓存并验真的选中文献");
    await updateJob(taskId, { status: "archiving", queueMode: "archive" });
    await addLog(taskId, "info", `开始归档 ${sequence.length} 篇已缓存 PDF`);
    const archivePlanned = async (planned) => {
      const current = await getRecord("items", planned.id);
      if (current?.state !== ITEM_STATE.PDF_CACHED) return current;
      return current.polarisTarget
        ? archivePolarisCachedOneLocalFirst(snapshot.job, current)
        : archiveCachedOne(snapshot.job, current);
    };
    if (snapshot.job.origin?.type === "polaris") {
      await runPolarisBatchWaves(sequence, archivePlanned, {
        maxConcurrency: 2,
        shouldStop: () => stopRequests.has(taskId),
      });
    } else {
      for (const planned of sequence.sort((left, right) => left.ordinal - right.ordinal)) {
        if (stopRequests.has(taskId)) break;
        await archivePlanned(planned);
      }
    }
    const finalSnapshot = await getJobSnapshot(taskId);
    if (stopRequests.has(taskId)) return finishStoppedTask(taskId);
    const completed = finalSnapshot.items.filter((item) => item.state === ITEM_STATE.COMPLETED).length;
    const cached = finalSnapshot.items.filter((item) => item.state === ITEM_STATE.PDF_CACHED).length;
    await updateJob(taskId, { status: cached ? "awaiting-archive" : "completed", queueMode: null });
    await addLog(taskId, "info", `归档结束：${completed} 篇已归档，${cached} 篇缓存待处理`);
    return { ok: true, completed, cached };
  })().finally(() => runningDownloads.delete(taskId));
  runningDownloads.set(taskId, task);
  return task;
}

async function retryItemAndResume(taskId, itemId) {
  let before = await getJobSnapshot(taskId);
  if (!before) throw new Error("下载任务不存在");
  const wasPausedItem = before.job.pausedItemId === itemId;
  if (before.job.queueMode === "registration" && before.job.pausedItemId && !wasPausedItem) {
    throw new Error("请先完成或放弃当前暂停的文献");
  }
  let queueMode = before.job.queueMode;
  if (wasPausedItem && !queueMode) {
    const pending = Array.isArray(before.job.pendingDownloadItemIds) && before.job.pendingDownloadItemIds.length
      ? before.job.pendingDownloadItemIds
      : [itemId];
    const job = await updateJob(taskId, {
      status: "awaiting-user",
      queueMode: "registration",
      queueItemIds: Array.isArray(before.job.queueItemIds) && before.job.queueItemIds.length
        ? before.job.queueItemIds
        : pending,
      pendingDownloadItemIds: pending.includes(itemId) ? pending : [itemId, ...pending],
    });
    before = { ...before, job };
    queueMode = "registration";
    await addLog(taskId, "info", "已恢复旧任务的顺序队列，当前文献成功后将自动处理下一篇");
  }
  if (wasPausedItem && queueMode === "registration") {
    const pending = new Set(before.job.pendingDownloadItemIds || [itemId]);
    await runRegistration(taskId, (item) => pending.has(item.id), true, itemId);
    const resumed = await getJobSnapshot(taskId);
    return { ok: true, resumed: resumed?.job?.pausedItemId !== itemId };
  }
  await runRegistration(taskId, (item) => item.id === itemId, true);
  const snapshot = await getJobSnapshot(taskId);
  if (!snapshot || !wasPausedItem || snapshot.job.pausedItemId !== itemId) return { ok: true, resumed: false };
  const item = snapshot.items.find((entry) => entry.id === itemId);
  if (item && registrationOutcomeAccepted(item)) {
    const patch = queuePatchAfterSettledItem(snapshot.job, itemId, snapshot.items);
    await updateJob(taskId, patch);
    const pending = patch.pendingDownloadItemIds;
    if (pending.length && queueMode === "cache") {
      const activeDownload = runningDownloads.get(taskId);
      if (activeDownload) await activeDownload.catch(() => null);
      await runCaching(taskId, pending);
    }
    return { ok: true, resumed: pending.length > 0 };
  }
  const resumable = item ? candidateCanUseBrowserCache(item, bestCandidate(item)) : false;
  const pending = Array.isArray(snapshot.job.pendingDownloadItemIds) ? snapshot.job.pendingDownloadItemIds : [];
  if (!resumable || !pending.length) return { ok: true, resumed: false };
  const activeDownload = runningDownloads.get(taskId);
  if (activeDownload) await activeDownload.catch(() => null);
  await runCaching(taskId, pending);
  return { ok: true, resumed: true };
}

async function stopActiveTaskWork(taskId) {
  const registration = runningRegistrations.get(taskId);
  const download = runningDownloads.get(taskId);
  if (!registration && !download) return;
  await requestTaskStop(taskId);
  await Promise.allSettled([registration, download].filter(Boolean));
}

async function reparseItemFromDoi(taskId, itemId) {
  await stopActiveTaskWork(taskId);
  const snapshot = await getJobSnapshot(taskId);
  if (!snapshot) throw new Error("下载任务不存在");
  const item = snapshot.items.find((entry) => entry.id === itemId);
  if (!item || item.taskId !== taskId) throw new Error("文献任务不存在");
  if (item.cache?.cacheKey) await deleteBrowserCachedPdf(item.cache.cacheKey);
  clearMetadataCache(item.doi);
  const reset = await persistItem(resetItemForDoiReparse(item));
  const pending = Array.isArray(snapshot.job.pendingDownloadItemIds)
    ? snapshot.job.pendingDownloadItemIds.filter((id) => id !== itemId)
    : [];
  await updateJob(taskId, {
    status: "queued",
    queueMode: "registration",
    pausedItemId: null,
    pendingDownloadItemIds: [itemId, ...pending],
  });
  await addLog(taskId, "info", `第 ${reset.ordinal} 篇已清理旧解析结果，正在从 ${reset.doi ? "DOI" : "文章地址"} 重新解析`);
  return runRegistration(taskId, (entry) => entry.id === itemId, true, itemId);
}

async function abandonItemAndContinue(taskId, itemId) {
  const before = await getJobSnapshot(taskId);
  if (!before) throw new Error("下载任务不存在");
  const item = before.items.find((entry) => entry.id === itemId);
  if (!item || item.taskId !== taskId) throw new Error("文献任务不存在");
  if (item.cache?.cacheKey && !item.file?.filename) await deleteBrowserCachedPdf(item.cache.cacheKey);
  const fileRetained = Boolean(item.file?.filename);
  const abandoned = await persistItem(transitionItem(item, ITEM_STATE.ABANDONED, {
    statusReason: fileRetained
      ? "用户已停止后续处理，核验文件保持在原归档位置"
      : "用户已放弃该文献，未保留下载文件",
    file: item.file || null,
  }));
  const snapshot = await getJobSnapshot(taskId);
  if (!snapshot) throw new Error("下载任务不存在");
  const wasCurrentPausedItem = snapshot.job.pausedItemId === itemId;
  const patch = queuePatchAfterAbandon(snapshot.job, itemId, snapshot.items.map((entry) => entry.id === itemId ? abandoned : entry));
  await updateJob(taskId, patch);
  const pending = patch.pendingDownloadItemIds;
  const shouldResume = wasCurrentPausedItem && pending.length > 0;
  const activeDownload = runningDownloads.get(taskId);
  if (shouldResume && activeDownload) await activeDownload.catch(() => null);
  if (shouldResume && snapshot.job.queueMode === "cache") await runCaching(taskId, pending);
  if (shouldResume && snapshot.job.queueMode === "registration") {
    const pendingSet = new Set(pending);
    await runRegistration(taskId, (entry) => pendingSet.has(entry.id), true);
  }
  return { ok: true, resumed: shouldResume };
}

async function handleChooseDestination(taskId) {
  const result = await sendNativeCommand(chrome, "choose_destination", {}, 120000);
  invalidateNativeBridgeStatus(chrome);
  const snapshot = await getJobSnapshot(taskId);
  if (!snapshot) throw new Error("下载任务不存在");
  await updateJob(taskId, {
    destination: {
      ...snapshot.job.destination,
      mode: "native-bridge",
      destinationId: result.destinationId,
      displayPath: result.displayPath,
    },
  });
  await setSetting(LAST_NATIVE_DESTINATION, {
    destinationId: result.destinationId,
    displayPath: result.displayPath,
  });
  return { ok: true, destinationId: result.destinationId, displayPath: result.displayPath };
}

function requireExtensionPage(sender) {
  const extensionRoot = chrome.runtime.getURL("");
  if (!sender?.url?.startsWith(extensionRoot)) throw new Error("该操作仅允许从插件界面发起");
}

function senderOrigin(sender) {
  try { return new URL(sender?.tab?.url || sender?.url || "").origin; } catch { return ""; }
}

function isAllowedPolarisOrigin(origin) {
  return origin === "https://polaris-lab.zeabur.app"
    || origin === "https://yfr.yangy.cn"
    || /^https?:\/\/(?:localhost|127\.0\.0\.1)(?::\d+)?$/.test(origin);
}

function isSafeScnetGuiLaunch(value) {
  try {
    const parsed = new URL(String(value || ""));
    return parsed.protocol === "https:" && (
      parsed.hostname === "scnet.cn"
      || parsed.hostname.endsWith(".scnet.cn")
      || parsed.hostname === "hpccube.com"
      || parsed.hostname.endsWith(".hpccube.com")
    );
  } catch {
    return false;
  }
}

async function callScnetGuiBridge(type, payload) {
  const tabs = await chrome.tabs.query({ url: ["https://www.scnet.cn/*", "https://scnet.cn/*"] });
  if (!tabs.length) throw new Error("请先在当前浏览器登录 SCNet，再返回 Polaris 重试");
  let lastError = "";
  for (const tab of tabs) {
    if (!Number.isInteger(tab.id)) continue;
    try {
      const result = await chrome.tabs.sendMessage(tab.id, { type, payload });
      if (result?.ok) return { result, scnetTabId: tab.id };
      lastError = result?.error || "SCNET_GUI_CONTROL_FAILED";
    } catch (error) {
      lastError = error instanceof Error ? error.message : "SCNet 页面通信失败";
    }
  }
  throw new Error(lastError || "SCNet 页面尚未加载 Polaris 扩展，请刷新 SCNet 标签页");
}

async function openScnetGui(payload, sender) {
  if (!isAllowedPolarisOrigin(senderOrigin(sender))) throw new Error("SCNet GUI 只能从 Polaris 页面发起");
  const { result } = await callScnetGuiBridge("SCNET_GUI_LAUNCH", payload);
  const launchUrl = String(result.launch_url || "");
  if (!isSafeScnetGuiLaunch(launchUrl)) throw new Error("SCNet 返回的 GUI 短期链接无效");
  const tab = await chrome.tabs.create({ url: launchUrl, active: true });
  return {
    ok: true,
    source: "extension",
    tabId: tab.id ?? null,
    job_id: result.job_id || null,
    scnet_session_id: result.scnet_session_id || null,
    template_version: result.template_version || null,
    state: result.state || "online",
  };
}

async function statusScnetGui(payload, sender) {
  if (!isAllowedPolarisOrigin(senderOrigin(sender))) throw new Error("SCNet GUI 状态只能从 Polaris 页面发起");
  const { result } = await callScnetGuiBridge("SCNET_GUI_STATUS", payload);
  return {
    ok: true,
    state: result.state || "not_found",
    job_id: result.job_id || null,
    scnet_session_id: result.scnet_session_id || null,
    template_version: result.template_version || null,
    app_type: result.app_type || null,
    started_at: result.started_at || null,
  };
}

async function collectScnetGui(payload, sender) {
  if (!isAllowedPolarisOrigin(senderOrigin(sender))) throw new Error("SCNet GUI 同步只能从 Polaris 页面发起");
  const tabs = await chrome.tabs.query({ url: ["https://www.scnet.cn/*", "https://scnet.cn/*"] });
  if (!tabs.length) throw new Error("请先打开同一 SCNet GUI 会话");
  for (const tab of tabs) {
    if (!Number.isInteger(tab.id)) continue;
    try {
      const result = await chrome.tabs.sendMessage(tab.id, { type: "SCNET_GUI_COLLECT", sessionId: payload?.session_id || null });
      if (result?.ok) return { ...result, tabId: tab.id };
    } catch { /* 页面可能尚未加载 content bridge，继续尝试其他标签 */ }
  }
  throw new Error("SCNet GUI 页面未提供可核验工作区清单");
}

function sanitizeScnetSnapshots(value) {
  if (!Array.isArray(value) || value.length < 1 || value.length > 500) {
    throw new Error("SCNet 模板快照为空或数量超限");
  }
  const forbidden = /(cookie|authorization|request.?headers|response.?headers|access.?key|secret.?key|token|password|user.?home|workdir|(^|_)path$)/i;
  return value.map((row) => {
    if (!row || typeof row !== "object") throw new Error("SCNet 模板快照格式无效");
    const allowed = {
      template_id: String(row.template_id || "").slice(0, 100),
      region_key: row.region_key == null ? null : String(row.region_key).slice(0, 255),
      submit_mode: String(row.submit_mode || "").slice(0, 32),
      source: "browser_bridge",
      status: String(row.status || "").slice(0, 48),
      evidence: row.evidence && typeof row.evidence === "object" ? row.evidence : {},
    };
    if (!/^\d{1,20}$/.test(allowed.template_id) || !allowed.status) throw new Error("SCNet 模板快照缺少必要字段");
    const encoded = JSON.stringify(allowed);
    if (forbidden.test(encoded)) throw new Error("SCNet 模板快照包含敏感字段");
    return allowed;
  });
}

async function saveScnetContext(message, sender) {
  const origin = senderOrigin(sender);
  if (!isAllowedPolarisOrigin(origin)) throw new Error("SCNet 凭据绑定必须来自 Polaris 页面");
  const credentialId = String(message.payload?.credentialId || "");
  if (!/^[0-9a-f-]{16,64}$/i.test(credentialId)) throw new Error("SCNet 凭据编号无效");
  let stored = await getSetting(POLARIS_CONNECTION, null);
  if (!stored?.apiKey || !stored?.instanceOrigin) throw new Error("请先配置 Polaris API Key");
  const instanceOrigin = String(message.payload?.instanceOrigin || stored.instanceOrigin);
  if (instanceOrigin !== stored.instanceOrigin) {
    stored = await rebindLoopbackPolarisConnection({ connection: stored, instanceOrigin });
    await setSetting(POLARIS_CONNECTION, stored);
  }
  const context = { credentialId, instanceOrigin: stored.instanceOrigin, updatedAt: new Date().toISOString() };
  await setSetting(SCNET_CONTEXT, context);
  return { ok: true, credentialId, instanceOrigin: context.instanceOrigin };
}

async function syncScnetSnapshot(message, sender) {
  const origin = senderOrigin(sender);
  if (!/^https:\/\/(?:www\.)?scnet\.cn$/.test(origin)) throw new Error("SCNet 模板快照来源无效");
  const connection = await getSetting(POLARIS_CONNECTION, null);
  const context = await getSetting(SCNET_CONTEXT, null);
  if (!connection?.apiKey || !connection.instanceOrigin) throw new Error("请先配置 Polaris API Key");
  const snapshots = sanitizeScnetSnapshots(message.snapshots);
  const result = await syncScnetSnapshots({
    instanceOrigin: connection.instanceOrigin,
    apiKey: connection.apiKey,
    credentialId: context?.credentialId,
    snapshots,
    nonce: message.nonce || null,
    accountFingerprint: message.accountFingerprint || null,
    purpose: message.purpose || "planning",
  });
  return { ...result, credentialId: context.credentialId, source: origin };
}

function requireCachedPdfViewer(sender) {
  requireExtensionPage(sender);
  const path = new URL(sender.url).pathname;
  if (path !== "/src/viewer/index.html") throw new Error("人工确认仅允许在缓存 PDF 查看页执行");
}

async function openCachedPdfViewer(taskId, itemId, sender) {
  requireExtensionPage(sender);
  const item = await getRecord("items", itemId);
  if (!item || item.taskId !== taskId) throw new Error("文献缓存记录不存在");
  if (!item.cache?.cacheKey || !(await getBrowserCachedPdf(item.cache.cacheKey))) {
    throw new Error("浏览器 PDF 缓存不存在或已被清理");
  }
  const viewer = new URL(chrome.runtime.getURL("src/viewer/index.html"));
  viewer.searchParams.set("taskId", taskId);
  viewer.searchParams.set("itemId", itemId);
  await chrome.tabs.create({ url: viewer.toString(), active: true });
  return { ok: true };
}

async function approveCachedPdf(taskId, itemId, sender) {
  requireCachedPdfViewer(sender);
  const item = await getRecord("items", itemId);
  if (!item || item.taskId !== taskId) throw new Error("文献缓存记录不存在");
  if (!item.cache?.cacheKey || !(await getBrowserCachedPdf(item.cache.cacheKey))) {
    throw new Error("浏览器 PDF 缓存不存在或已被清理");
  }
  if (![ITEM_STATE.VERIFICATION_INCONCLUSIVE, ITEM_STATE.QUARANTINED].includes(item.state)) {
    throw new Error("当前文献状态不允许人工确认");
  }
  const approval = { method: "user", approvedAt: new Date().toISOString(), source: "cached-pdf-viewer" };
  const approved = transitionItem(item, ITEM_STATE.PDF_CACHED, {
    identityApproval: approval,
    statusReason: "用户已查看浏览器缓存并人工确认文献身份；可重新归档",
    file: null,
  });
  await persistItem(approved);
  await addLog(taskId, "warn", `第 ${item.ordinal} 篇由用户查看缓存后人工确认身份`);
  return { ok: true, approvedAt: approval.approvedAt };
}

function launchBackgroundTask(taskId, operation, context) {
  void operation.catch(async (error) => {
    const message = publicError(error, context);
    try { await addLog(taskId, "error", message); } catch { /* The job may no longer exist. */ }
    try { await updateJob(taskId, { status: "failed", statusReason: message }); } catch { /* The job may no longer exist. */ }
    await notifyStateChanged(taskId, "background-error");
  });
}

async function handleMessage(message, sender) {
  const checked = validateMessage(message);
  if (!checked.ok) throw new Error(checked.error);
  switch (message.type) {
    case MESSAGE.YFR_IMPORT_SELECTION:
      return importYfrSelection(message, sender);
    case MESSAGE.POLARIS_IMPORT_TASK:
      return importPolarisTask(message, sender);
    case MESSAGE.POLARIS_IMPORT_BATCH:
      return importPolarisBatch(message, sender);
    case MESSAGE.TEST_POLARIS_CONNECTION: {
      const result = await testPolarisConnection(message.connection || {});
      return { ok: true, ...result };
    }
    case MESSAGE.SAVE_POLARIS_CONNECTION: {
      const result = await testPolarisConnection(message.connection || {});
      await setSetting(POLARIS_CONNECTION, {
        instanceOrigin: result.origin,
        apiKey: message.connection.apiKey,
        user: result.user,
        updatedAt: new Date().toISOString(),
      });
      return { ok: true, origin: result.origin, user: result.user };
    }
    case MESSAGE.AUTHORIZE_POLARIS_CONNECTION: {
      const origin = senderOrigin(sender);
      if (!isAllowedPolarisOrigin(origin) || message.pageOrigin !== origin) {
        throw new Error("扩展连接授权必须来自当前 Polaris 页面");
      }
      const requestedOrigin = new URL(String(message.payload?.instanceOrigin || "")).origin;
      if (requestedOrigin !== origin) throw new Error("扩展连接地址与当前 Polaris 页面不一致");
      const result = await testPolarisConnection(message.payload || {});
      await setSetting(POLARIS_CONNECTION, {
        instanceOrigin: result.origin,
        apiKey: message.payload.apiKey,
        user: result.user,
        updatedAt: new Date().toISOString(),
      });
      return { ok: true, origin: result.origin, user: result.user };
    }
    case MESSAGE.SCNET_SAVE_CONTEXT:
      return saveScnetContext(message, sender);
    case MESSAGE.SCNET_SYNC_SNAPSHOT:
      return syncScnetSnapshot(message, sender);
    case MESSAGE.SCNET_REFRESH: {
      if (!isAllowedPolarisOrigin(senderOrigin(sender))) {
        throw new Error("SCNet 重新校验只能从 Polaris 页面发起");
      }
      const tabs = Number.isInteger(message.tabId)
        ? [{ id: message.tabId }]
        : await chrome.tabs.query({
          url: ["https://www.scnet.cn/*", "https://scnet.cn/*"],
      });
      if (!tabs.length) throw new Error("请先在当前浏览器打开已登录的 SCNet 模板中心");
      const sentTabIds = [];
      let refreshError = "";
      for (const tab of tabs) {
        if (!Number.isInteger(tab.id)) continue;
        try {
          const result = await chrome.tabs.sendMessage(tab.id, {
            type: "SCNET_REFRESH",
            purpose: message.payload?.purpose || message.purpose || "planning",
          });
          if (!result?.ok) {
            refreshError = result?.error || "SCNet 模板同步失败";
            continue;
          }
          sentTabIds.push(tab.id);
        } catch (error) {
          refreshError = error instanceof Error ? error.message : "SCNet 页面通信失败";
          // Ignore stale tabs or pages that have not loaded the content bridge.
        }
      }
      if (!sentTabIds.length) {
        throw new Error(refreshError || "SCNet 页面尚未加载 Polaris 扩展，请刷新 SCNet 标签页");
      }
      return { ok: true, accepted: true, synced: true, tabIds: sentTabIds };
    }
    case MESSAGE.SCNET_GUI_OPEN:
      return openScnetGui(message.payload || {}, sender);
    case MESSAGE.SCNET_GUI_STATUS:
      return statusScnetGui(message.payload || {}, sender);
    case MESSAGE.SCNET_GUI_COLLECT:
      return collectScnetGui(message.payload || {}, sender);
    case MESSAGE.IMPORT_RECORDS:
      return importRecords(message);
    case MESSAGE.GET_STATE:
      if (message.taskId) await setCurrentTask(message.taskId);
      return { ok: true, ...(await currentState(message.taskId)) };
    case MESSAGE.START_REGISTRATION:
      if (!Array.isArray(message.itemIds) || !message.itemIds.length) throw new Error("请至少选择一篇文献进行预检");
      {
        const selected = new Set(message.itemIds);
        return runRegistration(message.taskId, (item) => selected.has(item.id), false);
      }
    case MESSAGE.START_CACHING:
      launchBackgroundTask(message.taskId, runCaching(message.taskId, Array.isArray(message.itemIds) ? message.itemIds : null), "批量缓存失败");
      return { ok: true, accepted: true };
    case MESSAGE.START_DOWNLOADS:
      launchBackgroundTask(message.taskId, runDownloads(message.taskId, Array.isArray(message.itemIds) ? message.itemIds : null), "批量归档失败");
      return { ok: true, accepted: true };
    case MESSAGE.STOP_TASK:
      return requestTaskStop(message.taskId);
    case MESSAGE.RESUME_TASK:
      return resumeStoppedTask(message.taskId);
    case MESSAGE.RETRY_ITEM:
      return retryItemAndResume(message.taskId, message.itemId);
    case MESSAGE.REPARSE_ITEM:
      return reparseItemFromDoi(message.taskId, message.itemId);
    case MESSAGE.ABANDON_ITEM:
      return abandonItemAndContinue(message.taskId, message.itemId);
    case MESSAGE.RECHECK_PUBLISHER: {
      if (!message.itemId) throw new Error("请指定当前文献进行复检");
      return retryItemAndResume(message.taskId, message.itemId);
    }
    case MESSAGE.OPEN_MANUAL_PAGE: {
      const item = await getRecord("items", message.itemId);
      if (!item) throw new Error("文献任务不存在");
      const manualTabId = item.articleTabId || item.manualTabId;
      if (manualTabId) {
        try {
          const existing = await chrome.tabs.get(manualTabId);
          const validScienceDirectArticle = item.publisherKey !== "sciencedirect"
            || (existing?.url && new URL(existing.url).hostname.endsWith("sciencedirect.com"));
          if (validScienceDirectArticle) {
            await chrome.tabs.update(manualTabId, { active: true });
            return { ok: true };
          }
        } catch { /* recreate below */ }
      }
      const url = item.articleUrl || (item.doi ? `https://doi.org/${item.doi}` : null);
      if (!parseSafeHttpUrl(url)) throw new Error("没有可打开的安全文章页面");
      const created = await chrome.tabs.create({ url, active: true });
      if (typeof created.id === "number") {
        await persistItem({
          ...item,
          manualTabId: created.id,
          ...(item.publisherKey === "sciencedirect" ? { articleTabId: created.id } : {}),
          statusReason: "已重新打开文章页，请完成验证后继续当前文献",
          updatedAt: new Date().toISOString(),
        });
      }
      return { ok: true };
    }
    case MESSAGE.OPEN_CACHED_PDF:
      return openCachedPdfViewer(message.taskId, message.itemId, sender);
    case MESSAGE.START_ASSISTED_PDF_CAPTURE:
      requireExtensionPage(sender);
      return startAssistedPdfCapture(message.taskId, message.itemId);
    case MESSAGE.STOP_ASSISTED_PDF_CAPTURE:
      requireExtensionPage(sender);
      return stopAssistedPdfCapture(message.taskId, message.itemId);
    case MESSAGE.APPROVE_CACHED_PDF:
      return approveCachedPdf(message.taskId, message.itemId, sender);
    case MESSAGE.CHOOSE_DESTINATION:
      return handleChooseDestination(message.taskId);
    case MESSAGE.UPDATE_SETTINGS: {
      const snapshot = await getJobSnapshot(message.taskId);
      if (!snapshot) throw new Error("下载任务不存在");
      if (runningDownloads.has(message.taskId) || ["caching", "archiving", "queued"].includes(snapshot.job.status)) {
        throw new Error("缓存或归档队列运行期间不能修改文件设置");
      }
      const destination = { ...snapshot.job.destination, ...(message.destination || {}) };
      await updateJob(message.taskId, {
        destination,
        limits: { ...snapshot.job.limits, ...(message.limits || {}) },
      });
      if (typeof message.destination?.namingTemplate === "string") {
        const updatedAt = new Date().toISOString();
        const renamedItems = snapshot.items.filter((item) => !item.file).map((item) => ({
            ...item,
            plannedFilename: plannedPdfFilename(destination.namingTemplate, {
              taskCode: snapshot.job.taskCode,
              ordinal: item.ordinal,
              title: item.title,
              doi: item.doi,
            }),
            updatedAt,
          }));
        await putRecords("items", renamedItems);
        await notifyStateChanged(message.taskId, "naming-template-updated");
      }
      return { ok: true };
    }
    case MESSAGE.DOWNLOAD_BRIDGE_INSTALLER: {
      const installerVersion = await getSetting("bridgeInstallerVersion", null);
      const previousId = installerVersion === MINIMUM_NATIVE_BRIDGE_VERSION
        ? await getSetting("bridgeInstallerDownloadId", null)
        : null;
      const installer = await startBridgeInstallerDownload(chrome, previousId);
      if (Number.isInteger(installer.downloadId)) {
        await setSetting("bridgeInstallerDownloadId", installer.downloadId);
        await setSetting("bridgeInstallerVersion", MINIMUM_NATIVE_BRIDGE_VERSION);
      }
      return { ok: true, installer };
    }
    case MESSAGE.OPEN_BRIDGE_INSTALLER: {
      const downloadId = await getSetting("bridgeInstallerDownloadId", null);
      return openBridgeInstaller(chrome, downloadId);
    }
    case MESSAGE.SHOW_BRIDGE_INSTALLER: {
      const downloadId = await getSetting("bridgeInstallerDownloadId", null);
      return showBridgeInstaller(chrome, downloadId);
    }
    case MESSAGE.REFRESH_BRIDGE_STATUS:
      invalidateNativeBridgeStatus(chrome);
      return { ok: true, bridge: await nativeBridgeStatus(chrome, { force: true }) };
    case MESSAGE.REFRESH_ZOTERO_STATUS:
      requireExtensionPage(sender);
      return { ok: true, zotero: await zoteroStatus(chrome, { force: true }) };
    case MESSAGE.PAIR_ZOTERO:
      requireExtensionPage(sender);
      return { ok: true, zotero: await pairWithZotero(chrome, message.pairingCode) };
    case MESSAGE.DISCONNECT_ZOTERO:
      requireExtensionPage(sender);
      await disconnectZotero(chrome);
      return { ok: true, zotero: await zoteroStatus(chrome, { force: true }) };
    case MESSAGE.UPDATE_ZOTERO_SETTINGS:
      requireExtensionPage(sender);
      {
        const zotero = await setZoteroAutoSync(chrome, message.autoSync);
        if (message.autoSync && zotero.available && zotero.paired) void schedulePendingZoteroRetry();
        return { ok: true, zotero };
      }
    case MESSAGE.RETRY_ZOTERO_ITEM:
      requireExtensionPage(sender);
      return retryZoteroSyncs({
        taskId: message.taskId,
        itemIds: [message.itemId],
        includeFailed: true,
        force: true,
      });
    case MESSAGE.RETRY_ZOTERO_PENDING:
      requireExtensionPage(sender);
      return retryZoteroSyncs({
        taskId: message.taskId || null,
        itemIds: Array.isArray(message.itemIds) ? message.itemIds : null,
        includeFailed: true,
        force: true,
      });
    default:
      throw new Error("消息尚未实现");
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  handleMessage(message, sender)
    .then((response) => sendResponse(response))
    .catch((error) => sendResponse({ ok: false, error: publicError(error) }));
  return true;
});

chrome.action.onClicked.addListener(async (tab) => {
  try { await handleActionClick(chrome, tab); } catch { /* Unsupported or closed tabs are ignored. */ }
});

chrome.runtime.onInstalled.addListener(async () => {
  try { await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }); } catch { /* older Chromium */ }
});

chrome.downloads.onChanged.addListener((delta) => {
  void (async () => {
    const installerId = await getSetting("bridgeInstallerDownloadId", null);
    if (delta.id !== installerId) return;
    await notifyStateChanged(await getSetting("currentTaskId", null), "bridge-installer-download");
  })();
});

void chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: false }).catch(() => {});

void (async () => {
  const jobs = await listJobs();
  for (const job of jobs.filter((item) => ["registering", "downloading"].includes(item.status))) {
    await updateJob(job.id, { status: "paused", pauseReason: "浏览器后台已重启，请手动继续" });
  }
  for (const job of jobs) {
    const snapshot = await getJobSnapshot(job.id);
    const stale = snapshot?.items.filter((item) => item.assistedCapture?.status === "listening") || [];
    for (const item of stale) {
      await persistItem({
        ...item,
        assistedCapture: null,
        assistedCaptureResult: {
          status: "interrupted",
          finishedAt: new Date().toISOString(),
          reason: "浏览器后台已重启，请重新启动 PDF 捕获",
        },
        statusReason: "浏览器后台已重启，请重新启动 PDF 捕获",
        updatedAt: new Date().toISOString(),
      });
    }
  }
})();
