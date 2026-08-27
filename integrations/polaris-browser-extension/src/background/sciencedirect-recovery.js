import { ITEM_STATE } from "../shared/constants.js";
import {
  findScienceDirectPdfTab,
  registerScienceDirectPdfAsset,
  scienceDirectAssetUsable,
} from "../publishers/sciencedirect-asset.js";
import { navigatePublisherPdf, probePublisherTabById } from "./tab-probe.js";

const MINIMUM_ASSET_REMAINING_MS = 90000;

function sameAssetPath(expectedUrl, candidateUrl) {
  try {
    const expected = new URL(expectedUrl);
    const candidate = new URL(candidateUrl);
    return expected.hostname === candidate.hostname && expected.pathname === candidate.pathname;
  } catch {
    return false;
  }
}

export function waitForScienceDirectPdfReadiness(chromeApi, match, {
  minimumWaitMs = 2500,
  quietPeriodMs = 1500,
  fallbackWaitMs = 12000,
  timeoutMs = 60000,
} = {}) {
  return new Promise((resolve) => {
    const startedAt = Date.now();
    const activeRequests = new Set();
    let sawCompletedResponse = false;
    let settled = false;
    let quietTimer;
    let fallbackTimer;
    let timeoutTimer;
    const webRequest = chromeApi?.webRequest;

    const cleanup = () => {
      clearTimeout(quietTimer);
      clearTimeout(fallbackTimer);
      clearTimeout(timeoutTimer);
      webRequest?.onBeforeRequest?.removeListener(onBeforeRequest);
      webRequest?.onCompleted?.removeListener(onCompleted);
      webRequest?.onErrorOccurred?.removeListener(onErrorOccurred);
    };
    const finish = (signal) => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve({ ...match, readinessSignal: signal, readyAt: new Date().toISOString() });
    };
    const scheduleQuietFinish = () => {
      clearTimeout(quietTimer);
      if (!sawCompletedResponse || activeRequests.size > 0) return;
      const minimumRemaining = Math.max(0, minimumWaitMs - (Date.now() - startedAt));
      quietTimer = setTimeout(() => finish("network-complete"), Math.max(quietPeriodMs, minimumRemaining));
    };
    const matches = (details) => sameAssetPath(match?.asset?.url, details?.url);
    const onBeforeRequest = (details) => {
      if (!matches(details)) return;
      activeRequests.add(details.requestId);
      clearTimeout(quietTimer);
    };
    const onCompleted = (details) => {
      if (!matches(details)) return;
      activeRequests.delete(details.requestId);
      if (Number(details.statusCode || 0) >= 200 && Number(details.statusCode || 0) < 400) {
        sawCompletedResponse = true;
      }
      scheduleQuietFinish();
    };
    const onErrorOccurred = (details) => {
      if (!matches(details)) return;
      activeRequests.delete(details.requestId);
      scheduleQuietFinish();
    };

    let filter = { urls: ["https://pdf.sciencedirectassets.com/*"] };
    try { filter = { urls: [`${new URL(match.asset.url).origin}/*`] }; } catch { /* Use the fixed publisher origin. */ }
    try {
      webRequest?.onBeforeRequest?.addListener(onBeforeRequest, filter);
      webRequest?.onCompleted?.addListener(onCompleted, filter);
      webRequest?.onErrorOccurred?.addListener(onErrorOccurred, filter);
    } catch {
      // Older Chromium builds fall back to a conservative fixed wait.
    }
    fallbackTimer = setTimeout(() => {
      if (activeRequests.size === 0) finish("settled-delay");
    }, fallbackWaitMs);
    timeoutTimer = setTimeout(() => finish("readiness-timeout"), Math.max(timeoutMs, fallbackWaitMs));
  });
}

async function matchingOpenAsset(chromeApi, item, preferredTabId = null) {
  let tabs = [];
  try { tabs = await chromeApi.tabs.query({}); } catch { /* Continue without a global scan. */ }
  const match = findScienceDirectPdfTab(item, tabs, { preferredTabId, now: new Date() });
  return match && match.tab.status !== "loading" && scienceDirectAssetUsable(match.asset, new Date(), MINIMUM_ASSET_REMAINING_MS) ? match : null;
}

function isScienceDirectArticleTab(tab) {
  try {
    const url = new URL(tab?.url || "");
    return url.hostname.endsWith("sciencedirect.com") && /^\/science\/article\/pii\/[a-z0-9]+\/?$/i.test(url.pathname);
  } catch {
    return false;
  }
}

export function scienceDirectViewPdfCandidate(item) {
  const existing = (item?.candidates || []).find((candidate) => {
    try {
      const url = new URL(candidate?.url || "");
      return url.hostname.endsWith("sciencedirect.com")
        && /^\/science\/article\/pii\/[a-z0-9]+\/pdfft\/?$/i.test(url.pathname);
    } catch {
      return false;
    }
  });
  if (existing) return { url: existing.url, actionId: existing.actionId || null };
  if (!isScienceDirectArticleTab({ url: item?.articleUrl })) return null;
  const articleUrl = new URL(item.articleUrl);
  articleUrl.pathname = `${articleUrl.pathname.replace(/\/$/, "")}/pdfft`;
  articleUrl.search = "";
  articleUrl.hash = "";
  return { url: articleUrl.toString(), actionId: null };
}

function waitForTabUrl(chromeApi, tabId, predicate, timeoutMs, pollIntervalMs = 250) {
  return new Promise((resolve, reject) => {
    let timer;
    let pollTimer;
    let settled = false;
    const cleanup = () => {
      clearTimeout(timer);
      clearTimeout(pollTimer);
      chromeApi.tabs.onUpdated?.removeListener(onUpdated);
      chromeApi.tabs.onRemoved?.removeListener(onRemoved);
    };
    const finish = (error, tab) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (error) reject(error);
      else resolve(tab);
    };
    const inspect = (tab) => {
      const url = tab?.pendingUrl || tab?.url;
      if (tab?.status !== "loading" && predicate(url)) finish(null, tab);
    };
    const poll = async () => {
      if (settled) return;
      try { inspect(await chromeApi.tabs.get(tabId)); } catch { /* onRemoved handles a closed tab. */ }
      if (!settled) pollTimer = setTimeout(poll, pollIntervalMs);
    };
    const onUpdated = (updatedId, changeInfo, tab) => {
      if (updatedId !== tabId) return;
      const url = changeInfo?.url || tab?.url;
      if (changeInfo?.status === "complete" && predicate(url)) finish(null, tab);
    };
    const onRemoved = (removedId) => {
      if (removedId === tabId) finish(new Error("ScienceDirect 文章标签页已关闭"));
    };
    chromeApi.tabs.onUpdated?.addListener(onUpdated);
    chromeApi.tabs.onRemoved?.addListener(onRemoved);
    timer = setTimeout(() => finish(new Error("ScienceDirect 文章页恢复超时，请刷新文章页后重试")), timeoutMs);
    void poll();
  });
}

function waitForMatchingAsset(chromeApi, item, articleTabId, action, timeoutMs = 45000) {
  return new Promise((resolve, reject) => {
    let timer;
    let settled = false;
    const pendingMatches = new Map();
    const cleanup = () => {
      clearTimeout(timer);
      chromeApi.tabs.onCreated?.removeListener(onCreated);
      chromeApi.tabs.onUpdated?.removeListener(onUpdated);
    };
    const finish = (error, match) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (error) reject(error);
      else resolve(match);
    };
    const inspect = (tab, ready = tab?.status !== "loading") => {
      const match = findScienceDirectPdfTab(item, [tab], { preferredTabId: articleTabId, now: new Date() });
      if (!match || !scienceDirectAssetUsable(match.asset, new Date(), MINIMUM_ASSET_REMAINING_MS)) return;
      if (ready) finish(null, match);
      else pendingMatches.set(match.tab.id, match);
    };
    const inspectAccessBarrier = async (tab) => {
      if (settled || typeof tab?.id !== "number") return;
      try {
        const snapshot = await probePublisherTabById(chromeApi, tab.id);
        if (snapshot?.access?.captcha) finish(new Error("ScienceDirect 需要完成人机验证，请在当前页面验证后重检"));
        else if (snapshot?.access?.loginRequired) finish(new Error("ScienceDirect 需要完成机构或个人登录，请登录后重检"));
      } catch {
        // PDF viewer and transient redirect pages are not publisher HTML pages.
      }
    };
    const onCreated = (tab) => inspect(tab);
    const onUpdated = (tabId, changeInfo, tab) => {
      if (changeInfo.status === "complete" && pendingMatches.has(tabId)) {
        const pending = pendingMatches.get(tabId);
        finish(null, { ...pending, tab: { ...pending.tab, ...tab, status: "complete" } });
        return;
      }
      if (changeInfo.url || changeInfo.status === "complete") {
        const current = { ...tab, pendingUrl: changeInfo.url || tab?.pendingUrl, url: changeInfo.url || tab?.url };
        inspect(
          current,
          changeInfo.status === "complete" || tab?.status === "complete",
        );
        if (changeInfo.status === "complete") void inspectAccessBarrier(current);
      }
    };
    chromeApi.tabs.onCreated?.addListener(onCreated);
    chromeApi.tabs.onUpdated?.addListener(onUpdated);
    timer = setTimeout(() => finish(new Error("等待 ScienceDirect PDF 跳转超时，请确认已完成人工验证")), timeoutMs);
    Promise.resolve()
      .then(action)
      .then(() => undefined)
      .catch((error) => finish(error));
  });
}

export async function captureScienceDirectPdfAfterVerification(chromeApi, item, timeoutMs = 45000) {
  const articleTabId = item?.articleTabId || item?.manualTabId;
  if (typeof articleTabId !== "number") throw new Error("ScienceDirect 文章标签页已丢失，请重新打开文章页");
  let articleTab;
  try { articleTab = await chromeApi.tabs.get(articleTabId); } catch { /* handled below */ }
  if (!isScienceDirectArticleTab(articleTab)) {
    const articleUrl = item?.articleUrl;
    if (!isScienceDirectArticleTab({ url: articleUrl })) {
      throw new Error("当前人工验证标签页不是 ScienceDirect 文章页，请重新打开后再验证");
    }
    await chromeApi.tabs.update(articleTabId, { url: articleUrl, active: true });
    articleTab = await waitForTabUrl(chromeApi, articleTabId, (url) => {
      try {
        return isScienceDirectArticleTab({ url });
      } catch {
        return false;
      }
    }, timeoutMs);
  }
  return waitForMatchingAsset(
    chromeApi,
    item,
    articleTabId,
    () => navigatePublisherPdf(chromeApi, articleTabId, scienceDirectViewPdfCandidate(item)),
    timeoutMs,
  );
}

export async function recoverScienceDirectPdfTab(
  chromeApi,
  item,
  now = new Date(),
  readinessOptions = {},
) {
  const tabs = [];
  if (typeof item.articleTabId === "number") {
    try { tabs.push(await chromeApi.tabs.get(item.articleTabId)); } catch { /* The article tab may have been replaced. */ }
  }
  if (typeof item.manualTabId === "number") {
    try { tabs.push(await chromeApi.tabs.get(item.manualTabId)); } catch { /* The manual tab may have been closed. */ }
  }
  try {
    tabs.push(...await chromeApi.tabs.query({}));
  } catch {
    // Continue with the known manual tab when tab querying is unavailable.
  }
  const uniqueTabs = Array.from(new Map(tabs.filter(Boolean).map((tab) => [tab.id, tab])).values());
  const match = findScienceDirectPdfTab(item, uniqueTabs, { preferredTabId: item.manualTabId, now });
  if (!match) return null;
  if (match.tab.status === "loading") return null;
  if (!scienceDirectAssetUsable(match.asset, now, MINIMUM_ASSET_REMAINING_MS)) {
    const articleTab = uniqueTabs.find((tab) => isScienceDirectArticleTab(tab));
    return {
      ...item,
      state: ITEM_STATE.MANUAL_REQUIRED,
      manualTabId: articleTab?.id || null,
      articleTabId: articleTab?.id || null,
      expiredAssetTabId: match.tab.id,
      statusReason: "ScienceDirect PDF 临时链接已过期或剩余时间不足，请重新打开文章页生成新的 View PDF",
      updatedAt: now.toISOString(),
    };
  }
  const readyMatch = await waitForScienceDirectPdfReadiness(chromeApi, match, readinessOptions);
  const recovered = registerScienceDirectPdfAsset(item, readyMatch, new Date());
  return { ...recovered, manualTabId: match.tab.id };
}
