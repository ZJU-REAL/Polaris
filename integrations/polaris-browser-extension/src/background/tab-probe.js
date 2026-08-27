import { MESSAGE } from "../shared/constants.js";
import { parseSafeHttpUrl } from "../shared/url-security.js";

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function sameUrlDocument(left, right) {
  try {
    const first = new URL(left);
    const second = new URL(right);
    return first.origin === second.origin && first.pathname === second.pathname;
  } catch {
    return false;
  }
}

function validProbeResponse(response) {
  return response?.ok && response.snapshot ? response.snapshot : null;
}

function snapshotCanProceed(snapshot, tab, acceptPartialSnapshot) {
  if (!snapshot) return false;
  if (tab?.status === "complete" || snapshot.documentReadyState === "complete") return true;
  if (snapshot.candidates?.length || Object.values(snapshot.access || {}).some(Boolean)) return true;
  return acceptPartialSnapshot && Boolean(snapshot.title || snapshot.doi || snapshot.canonicalUrl);
}

function readableSnapshotIdentity(snapshot) {
  if (!snapshot || !["interactive", "complete"].includes(snapshot.documentReadyState)) return "";
  const identity = [snapshot.canonicalUrl || snapshot.pageUrl, snapshot.doi, snapshot.title]
    .map((value) => String(value || "").trim())
    .filter(Boolean);
  return identity.length >= 2 ? identity.join("\n") : "";
}

export async function waitForPublisherPageReady(chromeApi, tabId, {
  timeoutMs = 30000,
  pollIntervalMs = 400,
  partialSnapshotGraceMs = 2000,
  acceptPartialSnapshot = false,
} = {}) {
  const deadline = Date.now() + Math.max(1000, Number(timeoutMs) || 30000);
  let latestTab = null;
  let partialIdentity = "";
  let partialSince = 0;
  while (Date.now() < deadline) {
    try {
      latestTab = await chromeApi.tabs.get(tabId);
    } catch {
      throw new Error("出版社页面在读取前已关闭");
    }
    let snapshot = null;
    try {
      snapshot = validProbeResponse(await chromeApi.tabs.sendMessage(tabId, { type: MESSAGE.PROBE_PUBLISHER_PAGE }));
    } catch {
      // The document or its content script may still be starting.
    }
    if (snapshotCanProceed(snapshot, latestTab, acceptPartialSnapshot)) {
      return { tab: latestTab, snapshot };
    }
    const identity = readableSnapshotIdentity(snapshot);
    if (identity) {
      if (identity !== partialIdentity) {
        partialIdentity = identity;
        partialSince = Date.now();
      } else if (Date.now() - partialSince >= Math.max(250, Number(partialSnapshotGraceMs) || 2000)) {
        return { tab: latestTab, snapshot };
      }
    } else {
      partialIdentity = "";
      partialSince = 0;
    }
    if (latestTab.status === "complete") {
      return { tab: latestTab, snapshot: await probePublisherTabById(chromeApi, tabId) };
    }
    await delay(Math.min(Math.max(50, Number(pollIntervalMs) || 400), Math.max(0, deadline - Date.now())));
  }
  throw new Error("出版社页面加载超时");
}

async function sendPublisherMessage(chromeApi, tabId, message) {
  try {
    const response = await chromeApi.tabs.sendMessage(tabId, message);
    if (response !== undefined) return response;
  } catch {
    // A newly navigated document may not have the publisher probe yet.
  }
  try {
    await chromeApi.scripting.executeScript({ target: { tabId }, files: ["src/content/publisher-probe.js"] });
    return await chromeApi.tabs.sendMessage(tabId, message);
  } catch {
    throw new Error("当前出版社尚未授权页面读取，请在打开的标签页中手动检查");
  }
}

async function sendProbe(chromeApi, tabId) {
  return sendPublisherMessage(chromeApi, tabId, { type: MESSAGE.PROBE_PUBLISHER_PAGE });
}

async function waitForPublisherDocument(chromeApi, tabId, expectedUrl, timeoutMs) {
  const deadline = Date.now() + Math.max(1000, Number(timeoutMs) || 30000);
  while (Date.now() < deadline) {
    let tab = null;
    try {
      tab = await chromeApi.tabs.get(tabId);
    } catch {
      throw new Error("出版社页面在 PDF 导航期间已关闭");
    }
    try {
      const snapshot = validProbeResponse(await sendProbe(chromeApi, tabId));
      const documentUrl = snapshot?.pageUrl || tab?.url;
      if (snapshot && sameUrlDocument(documentUrl, expectedUrl)) return { tab, snapshot };
    } catch {
      // The ePDF document or its content script may still be loading.
    }
    await delay(Math.min(250, Math.max(0, deadline - Date.now())));
  }
  throw new Error("出版社 PDF 阅读页加载超时，请在当前标签页完成验证后重新解析");
}

export async function probePublisherTabById(chromeApi, tabId) {
  if (typeof tabId !== "number") throw new Error("出版社文章标签页无效");
  const response = await sendProbe(chromeApi, tabId);
  if (!response?.ok || !response.snapshot) throw new Error(response?.error || "出版社页面未返回可用元数据");
  return response.snapshot;
}

export async function preflightPublisherCandidateInTab(chromeApi, tabId, url, timeoutMs = 15000) {
  const safeUrl = parseSafeHttpUrl(url);
  if (!safeUrl || typeof tabId !== "number") throw new Error("PDF 预检标签页或地址无效");
  const response = await sendPublisherMessage(chromeApi, tabId, {
    type: MESSAGE.PREFLIGHT_PDF_CANDIDATE,
    url: safeUrl,
    timeoutMs,
  });
  if (!response?.ok || !response.result) throw new Error(response?.error || "出版社标签页未返回 PDF 预检结果");
  return response.result;
}

export async function navigatePublisherPdf(chromeApi, tabId, candidate = null) {
  if (typeof tabId !== "number") throw new Error("出版社文章标签页无效");
  const requestedUrl = candidate?.navigationUrl || candidate?.url;
  const url = requestedUrl ? parseSafeHttpUrl(requestedUrl) : null;
  const finalUrl = candidate?.url ? parseSafeHttpUrl(candidate.url) : null;
  if (candidate?.directReader && url) {
    const tab = await chromeApi.tabs.get(tabId);
    if (sameUrlDocument(tab?.pendingUrl || tab?.url, url)) {
      await chromeApi.tabs.reload(tabId);
      return { url, action: "direct-reader-reload" };
    }
    await chromeApi.tabs.update(tabId, { url });
    return { url, action: "direct-reader-navigation" };
  }
  if (requestedUrl && !url) throw new Error("PDF 入口地址无效");
  let response = await sendPublisherMessage(chromeApi, tabId, {
    type: MESSAGE.NAVIGATE_PUBLISHER_PDF,
    url,
    actionId: typeof candidate?.actionId === "string" ? candidate.actionId.slice(0, 120) : null,
  });
  if (!response?.ok || !response.url) throw new Error(response?.error || "出版社页面没有返回 PDF 入口");
  if (candidate?.navigationUrl && finalUrl && !sameUrlDocument(url, finalUrl)) {
    await waitForPublisherDocument(chromeApi, tabId, url, candidate.navigationTimeoutMs);
    response = await sendPublisherMessage(chromeApi, tabId, {
      type: MESSAGE.NAVIGATE_PUBLISHER_PDF,
      url: finalUrl,
      actionId: null,
    });
    if (!response?.ok || !response.url) {
      throw new Error(response?.error || "出版社 PDF 阅读页未能触发真实 PDF 响应");
    }
  }
  return { url: response.url, action: response.action || "navigate" };
}

export async function probePublisherTab(chromeApi, url, {
  active = false,
  timeoutMs = 30000,
  acceptPartialSnapshot = false,
} = {}) {
  const safeUrl = parseSafeHttpUrl(url);
  if (!safeUrl) throw new Error("出版社页面地址不安全或无效");
  const tab = await chromeApi.tabs.create({ url: safeUrl, active });
  if (typeof tab.id !== "number") throw new Error("无法创建出版社页面标签页");
  let currentTab = tab;
  try {
    if (tab.status === "complete") {
      const snapshot = await probePublisherTabById(chromeApi, tab.id);
      return { tabId: tab.id, tab, snapshot };
    }
    const ready = await waitForPublisherPageReady(chromeApi, tab.id, { timeoutMs, acceptPartialSnapshot });
    currentTab = ready.tab;
    return { tabId: tab.id, tab: ready.tab, snapshot: ready.snapshot };
  } catch (error) {
    try { currentTab = await chromeApi.tabs.get(tab.id); } catch { /* Preserve the latest known tab snapshot. */ }
    return { tabId: tab.id, tab: currentTab, error };
  }
}

export async function closeTabQuietly(chromeApi, tabId) {
  try {
    await chromeApi.tabs.remove(tabId);
  } catch {
    // A user may already have closed the tab.
  }
}
