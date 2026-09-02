import { archivePdfToPolaris, testPolarisConnection } from "./api.js";
import { registerPolarisBridge, requireUrlPermission } from "./bridge.js";
import { deleteCachedPdf, fetchAndCachePdf, getCachedPdf } from "./cache.js";
import { getConnection, getTask, listTasks, putTask, removeTask, saveConnection, updateItem } from "./storage.js";
import { createLocalTask, validateDownloadBatch } from "../shared/batch.js";
import { normalizePolarisOrigin, originsMatch, permissionPatternForUrl } from "../shared/origin.js";

function messageError(error) {
  return error instanceof Error ? error.message : String(error || "操作失败");
}

async function publicState() {
  const connection = await getConnection();
  return {
    connection: connection ? {
      instanceOrigin: connection.instanceOrigin,
      user: connection.user,
      hasApiKey: Boolean(connection.apiKey),
      updatedAt: connection.updatedAt,
    } : null,
    tasks: await listTasks(),
  };
}

async function connectionInput(payload) {
  const existing = await getConnection();
  const instanceOrigin = normalizePolarisOrigin(payload?.instanceOrigin || existing?.instanceOrigin);
  const suppliedApiKey = String(payload?.apiKey || "").trim();
  const canReuseExisting = existing && originsMatch(existing.instanceOrigin, instanceOrigin);
  const apiKey = suppliedApiKey || (canReuseExisting ? existing.apiKey : "");
  return { instanceOrigin, apiKey };
}

async function connect(payload, save) {
  const existing = await getConnection();
  const input = await connectionInput(payload);
  const verified = await testPolarisConnection(input);
  if (save) {
    const bridgeRegistered = await registerPolarisBridge(input.instanceOrigin);
    if (!bridgeRegistered) throw new Error("需要授权访问该 Polaris 地址");
    await saveConnection({ ...input, user: verified.user });
    if (existing?.instanceOrigin && !originsMatch(existing.instanceOrigin, input.instanceOrigin)) {
      const pattern = permissionPatternForUrl(existing.instanceOrigin);
      await chrome.permissions.remove({ origins: [pattern] });
    }
  }
  return { ok: true, origin: verified.origin, user: verified.user };
}

async function importBatch(message) {
  const connection = await getConnection();
  if (!connection?.apiKey) throw new Error("请先在扩展中连接 Polaris");
  if (!originsMatch(connection.instanceOrigin, message.pageOrigin)) {
    throw new Error("当前页面与扩展连接的 Polaris 实例不一致");
  }
  const validated = validateDownloadBatch(message.payload, message.pageOrigin);
  if (!validated.ok) throw new Error(validated.error);
  const tasks = await listTasks();
  const duplicate = tasks.find((task) => task.batchNonce === validated.value.batchNonce);
  if (duplicate) return { ok: true, taskId: duplicate.id, count: duplicate.items.length, duplicate: true };
  const task = createLocalTask(validated.value);
  await putTask(task);
  return { ok: true, taskId: task.id, count: task.items.length };
}

async function taskItem(taskId, itemId) {
  const task = await getTask(taskId);
  const item = task?.items.find((entry) => entry.id === itemId);
  if (!task || !item) throw new Error("下载任务中的论文不存在");
  return { task, item };
}

async function downloadItem(payload) {
  const { task, item } = await taskItem(payload.taskId, payload.itemId);
  const candidate = item.candidates.find((entry) => entry.url === payload.url) || item.candidates[0];
  if (!candidate) throw new Error("该论文没有可用的 PDF 候选地址");
  await requireUrlPermission(candidate.url);
  await updateItem(task.id, item.id, { ...item, status: "downloading", error: null });
  try {
    const cache = await fetchAndCachePdf({ taskId: task.id, itemId: item.id, url: candidate.url });
    await updateItem(task.id, item.id, (current) => ({ ...current, status: "cached", error: null, cache }));
    return { ok: true, cache };
  } catch (error) {
    await updateItem(task.id, item.id, (current) => ({ ...current, status: "failed", error: messageError(error) }));
    throw error;
  }
}

async function acknowledgeManualCache(payload) {
  const { task, item } = await taskItem(payload.taskId, payload.itemId);
  if (!payload.cache?.cacheKey || !payload.cache?.sha256 || !payload.cache?.byteSize) {
    throw new Error("手动 PDF 缓存信息不完整");
  }
  await updateItem(task.id, item.id, { ...item, status: "cached", error: null, cache: payload.cache });
  return { ok: true };
}

async function archiveItem(taskId, itemId) {
  const { task, item } = await taskItem(taskId, itemId);
  if (!item.cache?.cacheKey) throw new Error("请先缓存该论文 PDF");
  const connection = await getConnection();
  if (!connection || !originsMatch(connection.instanceOrigin, task.instanceOrigin)) {
    throw new Error("任务来源与当前 Polaris 连接不一致");
  }
  const cachedResponse = await getCachedPdf(item.cache.cacheKey);
  await updateItem(task.id, item.id, { ...item, status: "archiving", error: null });
  try {
    const archive = await archivePdfToPolaris({ connection, item, cachedResponse });
    await updateItem(task.id, item.id, (current) => ({ ...current, status: "archived", error: null, archive }));
    return { ok: true, archive };
  } catch (error) {
    await updateItem(task.id, item.id, (current) => ({ ...current, status: "failed", error: messageError(error) }));
    throw error;
  }
}

async function archiveTask(taskId) {
  const task = await getTask(taskId);
  if (!task) throw new Error("下载任务不存在");
  const results = [];
  for (const item of task.items) {
    if (!item.cache?.cacheKey || item.status === "archived") continue;
    try {
      await archiveItem(taskId, item.id);
      results.push({ itemId: item.id, ok: true });
    } catch (error) {
      results.push({ itemId: item.id, ok: false, error: messageError(error) });
    }
  }
  return {
    ok: results.some((result) => result.ok),
    archived: results.filter((result) => result.ok).length,
    failed: results.filter((result) => !result.ok).length,
  };
}

async function deleteTask(taskId) {
  const removed = await removeTask(taskId);
  for (const item of removed?.items || []) await deleteCachedPdf(item.cache?.cacheKey);
  return { ok: true };
}

async function handleMessage(message) {
  switch (message?.type) {
    case "GET_STATE": return { ok: true, ...(await publicState()) };
    case "TEST_CONNECTION": return connect(message.payload, false);
    case "SAVE_CONNECTION": return connect(message.payload, true);
    case "POLARIS_IMPORT_BATCH": return importBatch(message);
    case "DOWNLOAD_ITEM": return downloadItem(message.payload);
    case "MANUAL_PDF_CACHED": return acknowledgeManualCache(message.payload);
    case "ARCHIVE_ITEM": return archiveItem(message.payload.taskId, message.payload.itemId);
    case "ARCHIVE_TASK": return archiveTask(message.payload.taskId);
    case "DELETE_TASK": return deleteTask(message.payload.taskId);
    case "OPEN_ARTICLE":
      await chrome.tabs.create({ url: message.payload.url });
      return { ok: true };
    default: throw new Error("不支持的扩展操作");
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  handleMessage(message)
    .then((result) => sendResponse(result))
    .catch((error) => sendResponse({ ok: false, error: messageError(error) }));
  return true;
});

async function restoreBridge() {
  const connection = await getConnection();
  if (connection?.instanceOrigin) await registerPolarisBridge(connection.instanceOrigin);
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => undefined);
  restoreBridge().catch(() => undefined);
});
chrome.runtime.onStartup.addListener(() => restoreBridge().catch(() => undefined));
