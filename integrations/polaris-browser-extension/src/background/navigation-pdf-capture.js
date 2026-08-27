import { parseScienceDirectPdfAsset } from "../publishers/sciencedirect-asset.js";

const PDF_SIGNATURE = "%PDF-";

function normalizedPii(value) {
  return String(value || "").trim().toUpperCase();
}

function sameUrlPath(left, right) {
  try {
    const a = new URL(left);
    const b = new URL(right);
    return a.origin === b.origin && a.pathname === b.pathname;
  } catch {
    return false;
  }
}

function urlPathKey(value) {
  try {
    const url = new URL(value);
    return `${url.origin}${url.pathname}`;
  } catch {
    return null;
  }
}

function headerValue(headers, name) {
  const target = String(name).toLowerCase();
  for (const [key, value] of Object.entries(headers || {})) {
    if (key.toLowerCase() === target) return String(value || "");
  }
  return "";
}

function decodeBody(body, base64Encoded) {
  if (!base64Encoded) return new TextEncoder().encode(String(body || ""));
  const binary = atob(String(body || ""));
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

function encodeBody(bytes) {
  let binary = "";
  const block = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += block) {
    binary += String.fromCharCode(...bytes.subarray(offset, Math.min(bytes.length, offset + block)));
  }
  return btoa(binary);
}

function hasPdfSignature(bytes) {
  if (!(bytes instanceof Uint8Array) || bytes.length < PDF_SIGNATURE.length) return false;
  return new TextDecoder("ascii").decode(bytes.slice(0, PDF_SIGNATURE.length)) === PDF_SIGNATURE;
}

function fulfillHeaders(headers, byteLength, inlinePdf = false) {
  const excluded = new Set(["content-encoding", "content-length", "transfer-encoding"]);
  if (inlinePdf) {
    excluded.add("content-disposition");
    excluded.add("content-type");
  }
  const output = (Array.isArray(headers) ? headers : [])
    .filter((header) => !excluded.has(String(header?.name || "").toLowerCase()));
  output.push({ name: "content-length", value: String(byteLength) });
  if (inlinePdf) {
    output.push({ name: "content-type", value: "application/pdf" });
    output.push({ name: "content-disposition", value: "inline" });
  }
  return output;
}

function parsedContentRange(headers) {
  const values = Object.fromEntries((headers || []).map((header) => [header.name, header.value]));
  const match = headerValue(values, "content-range").match(/^bytes\s+0-(\d+)\/(\d+)$/i);
  if (!match) {
    const general = headerValue(values, "content-range").match(/^bytes\s+(\d+)-(\d+)\/(\d+)$/i);
    if (!general) return null;
    const [, start, end, total] = general.map(Number);
    return { start, end, total };
  }
  return { start: 0, end: Number(match[1]), total: Number(match[2]) };
}

function mergeRange(ranges, start, end) {
  const sorted = [...ranges, { start, end }].sort((left, right) => left.start - right.start);
  const merged = [];
  for (const range of sorted) {
    const previous = merged.at(-1);
    if (!previous || range.start > previous.end + 1) merged.push({ ...range });
    else previous.end = Math.max(previous.end, range.end);
  }
  return merged;
}

export async function startNavigationPdfCapture(chromeApi, {
  initialTabId,
  expectedPii = null,
  expectedUrl = null,
  maxBytes,
  timeoutMs = 120000,
  allowAnyHttpPdf = false,
  allowUnboundDocument = false,
  allowUnboundPdfResponse = false,
  failOnAccessDenied = false,
  expectedQueryParams = null,
  onDownloadCandidate = null,
} = {}) {
  if (!chromeApi?.debugger?.attach || !chromeApi?.debugger?.sendCommand) {
    throw new Error("浏览器不支持 PDF 导航响应捕获，请重新加载新版扩展");
  }
  if (typeof initialTabId !== "number") throw new Error("缺少可监听的出版社标签页");
  const limit = Math.max(1024, Number(maxBytes || 0));
  const pii = normalizedPii(expectedPii);
  const attachedTabs = new Set();
  const attachingTabs = new Map();
  let rangeBuffer = null;
  let rangeTotal = 0;
  let receivedRanges = [];
  let capturedResourceUrl = null;
  const trustedNetworkIds = new Set();
  const trustedUrlPaths = new Set();
  const requiredQueryParams = Object.entries(expectedQueryParams || {})
    .slice(0, 8)
    .map(([key, value]) => [String(key), String(value)]);
  const expectedUrlPath = urlPathKey(expectedUrl);
  if (expectedUrlPath && !requiredQueryParams.length) trustedUrlPaths.add(expectedUrlPath);
  let settled = false;
  let closed = false;
  let resolveCapture;
  let rejectCapture;
  let lastReadError = null;
  let initialPageUrl = null;

  const captured = new Promise((resolve, reject) => {
    resolveCapture = resolve;
    rejectCapture = reject;
  });
  // The caller may fail before it starts waiting (for example while restoring
  // an article tab). Observe the rejection immediately while preserving it for
  // waitForPdf(), so close() cannot create an unhandled promise rejection.
  captured.catch(() => undefined);

  const trustResponse = (url, networkId = null) => {
    const path = urlPathKey(url);
    if (path) trustedUrlPaths.add(path);
    if (networkId) trustedNetworkIds.add(networkId);
  };

  const matchesExpectedUrl = (url) => {
    if (!expectedUrl || !sameUrlPath(expectedUrl, url)) return false;
    if (!requiredQueryParams.length) return true;
    try {
      const parsed = new URL(url);
      return requiredQueryParams.every(([key, value]) => parsed.searchParams.get(key) === value);
    } catch {
      return false;
    }
  };

  const matchesUrl = (url, resourceType = "", networkId = null) => {
    if (capturedResourceUrl && sameUrlPath(capturedResourceUrl, url)) return true;
    if (matchesExpectedUrl(url)) return true;
    const asset = parseScienceDirectPdfAsset(url);
    if (asset && (!pii || normalizedPii(asset.pii) === pii)) return true;
    if (!allowAnyHttpPdf) return false;
    try {
      const parsed = new URL(url);
      if (!["http:", "https:"].includes(parsed.protocol)) return false;
      if (networkId && trustedNetworkIds.has(networkId)) return true;
      if (trustedUrlPaths.has(`${parsed.origin}${parsed.pathname}`)) return true;
      return allowUnboundDocument && resourceType === "Document";
    } catch {
      return false;
    }
  };

  const finish = (error, value) => {
    if (settled) return;
    settled = true;
    clearTimeout(timeoutTimer);
    if (error) rejectCapture(error);
    else resolveCapture(value);
  };

  const attachTab = async (tabId) => {
    if (typeof tabId !== "number" || attachedTabs.has(tabId)) return;
    if (attachingTabs.has(tabId)) return attachingTabs.get(tabId);
    const pending = (async () => {
      await chromeApi.debugger.attach({ tabId }, "1.3");
      attachedTabs.add(tabId);
      let fetchPattern = allowAnyHttpPdf ? "*" : "https://pdf.sciencedirectassets.com/*";
      if (expectedUrl && !allowAnyHttpPdf) {
        try { fetchPattern = `${new URL(expectedUrl).origin}/*`; } catch { /* Use the publisher origin. */ }
      }
      await chromeApi.debugger.sendCommand({ tabId }, "Fetch.enable", {
        patterns: [{ urlPattern: fetchPattern, requestStage: "Response" }],
      });
    })().finally(() => attachingTabs.delete(tabId));
    attachingTabs.set(tabId, pending);
    return pending;
  };

  const continueFetch = async (source, requestId) => {
    try {
      await chromeApi.debugger.sendCommand(source, "Fetch.continueResponse", { requestId });
    } catch {
      try {
        await chromeApi.debugger.sendCommand(source, "Fetch.continueRequest", { requestId });
      } catch {
        // The request may already have continued after a navigation replacement.
      }
    }
  };

  const capturePausedResponse = async (source, params) => {
    const responseCode = Number(params?.responseStatusCode || 0);
    const requestUrl = params?.request?.url;
    const networkId = params?.networkId || null;
    const headers = Array.isArray(params.responseHeaders) ? params.responseHeaders : [];
    const headerObject = Object.fromEntries(headers.map((header) => [header.name, header.value]));
    const contentType = headerValue(headerObject, "content-type");
    const contentDisposition = headerValue(headerObject, "content-disposition");
    const hasPdfResponseMetadata = /application\/pdf|application\/octet-stream/i.test(contentType)
      || /filename\*?\s*=.*\.pdf(?:["';\s]|$)/i.test(contentDisposition);
    const unboundPdfResponse = allowUnboundPdfResponse
      && [200, 206].includes(responseCode)
      && hasPdfResponseMetadata
      && ["Document", "XHR", "Fetch", "Other"].includes(String(params?.resourceType || ""));
    if ((!matchesUrl(requestUrl, params?.resourceType, networkId) && !unboundPdfResponse) || !responseCode) {
      await continueFetch(source, params.requestId);
      return;
    }
    trustResponse(requestUrl, networkId);
    if (responseCode >= 300 && responseCode < 400) {
      const values = Object.fromEntries(headers.map((header) => [header.name, header.value]));
      const location = headerValue(values, "location");
      if (location) {
        try { trustResponse(new URL(location, requestUrl).toString(), networkId); } catch { /* Invalid redirects remain untrusted. */ }
      }
      await continueFetch(source, params.requestId);
      return;
    }
    if (![200, 206].includes(responseCode)) {
      lastReadError = new Error(`浏览器尚未取得 PDF 响应（HTTP ${responseCode}）`);
      await continueFetch(source, params.requestId);
      if (failOnAccessDenied && [401, 403, 418].includes(responseCode)) finish(lastReadError);
      return;
    }
    try {
      const result = await chromeApi.debugger.sendCommand(source, "Fetch.getResponseBody", {
        requestId: params.requestId,
      });
      const bytes = decodeBody(result?.body, Boolean(result?.base64Encoded));
      const inlinePdf = hasPdfSignature(bytes)
        || (responseCode === 206 && /application\/pdf/i.test(headerValue(headerObject, "content-type")));
      await chromeApi.debugger.sendCommand(source, "Fetch.fulfillRequest", {
        requestId: params.requestId,
        responseCode,
        responsePhrase: params.responseStatusText || undefined,
        responseHeaders: fulfillHeaders(headers, bytes.byteLength, inlinePdf),
        body: encodeBody(bytes),
      });
      if (responseCode === 206) {
        const range = parsedContentRange(headers);
        if (!range || range.start < 0 || range.end < range.start || range.total <= range.end
          || bytes.byteLength !== range.end - range.start + 1) {
        lastReadError = new Error("出版社返回了无效的 PDF 分段响应");
          return;
        }
        if (range.total > limit) throw new Error("PDF 超过任务配置的大小上限");
        capturedResourceUrl = requestUrl;
        if (!rangeBuffer || rangeTotal !== range.total) {
          rangeBuffer = new Uint8Array(range.total);
          rangeTotal = range.total;
          receivedRanges = [];
        }
        rangeBuffer.set(bytes, range.start);
        receivedRanges = mergeRange(receivedRanges, range.start, range.end);
        const complete = receivedRanges.length === 1
          && receivedRanges[0].start === 0
          && receivedRanges[0].end === range.total - 1;
        if (!complete) {
          lastReadError = new Error("正在拼装浏览器 PDF 分段响应");
          return;
        }
        if (!hasPdfSignature(rangeBuffer)) throw new Error("浏览器 PDF 分段响应缺少 %PDF- 文件签名");
        finish(null, {
          bytes: rangeBuffer,
          mime: "application/pdf",
          sourceUrl: params.request.url,
          tabId: source.tabId,
          requestId: params.requestId,
        });
        return;
      }
      if (responseCode !== 200 || bytes.byteLength <= 1024 || !hasPdfSignature(bytes)) {
        const responseText = new TextDecoder().decode(bytes.slice(0, 65536));
        const accessPage = /captcha|verify you are human|human verification|radware|botmanager|validate\.perfdrive|aliyun_waf|acw_sc__v2|access denied|forbidden|sign in|institutional login/i.test(responseText);
        lastReadError = new Error(accessPage
          ? "出版社返回了登录或人机验证页面"
          : `浏览器 PDF 响应尚不完整（HTTP ${responseCode}）`);
        if (failOnAccessDenied && accessPage) finish(lastReadError);
        return;
      }
      if (bytes.byteLength > limit) throw new Error("PDF 超过任务配置的大小上限");
      capturedResourceUrl = requestUrl;
      finish(null, {
        bytes,
        mime: headerValue(headerObject, "content-type") || "application/pdf",
        sourceUrl: params.request.url,
        tabId: source.tabId,
        requestId: params.requestId,
      });
    } catch (error) {
      lastReadError = error;
      await continueFetch(source, params.requestId);
    }
  };

  const onDebuggerEvent = (source, method, params) => {
    if (!attachedTabs.has(source?.tabId)) return;
    if (method === "Fetch.requestPaused") {
      void capturePausedResponse(source, params);
    }
  };

  const onTabCreated = (tab) => {
    if (tab?.openerTabId === initialTabId || attachedTabs.has(tab?.openerTabId)) {
      void attachTab(tab.id).catch((error) => { lastReadError = error; });
    }
  };
  const onTabUpdated = (tabId, changeInfo, tab) => {
    const url = changeInfo?.url || tab?.pendingUrl || tab?.url;
    if (matchesUrl(url, "Document")) void attachTab(tabId).catch((error) => { lastReadError = error; });
  };

  const downloadBelongsToCapture = (download) => {
    if (typeof download?.tabId === "number") return attachedTabs.has(download.tabId);
    const referrer = String(download?.referrer || "");
    if (!referrer || !initialPageUrl) return false;
    try {
      const referrerUrl = new URL(referrer);
      const pageUrl = new URL(initialPageUrl);
      return referrerUrl.origin === pageUrl.origin;
    } catch {
      return false;
    }
  };

  const onDownloadCreated = (download) => {
    if (!downloadBelongsToCapture(download)) return;
    const sourceUrl = String(download?.finalUrl || download?.url || "");
    const filename = String(download?.filename || "");
    const mime = String(download?.mime || "");
    if (!/\.pdf(?:$|[?#])/i.test(sourceUrl)
      && !/\.pdf$/i.test(filename)
      && !/application\/pdf|application\/octet-stream/i.test(mime)) return;
    trustResponse(sourceUrl);
    try {
      onDownloadCandidate?.({
        downloadId: download.id,
        sourceUrl,
        filename,
        mime,
        tabId: typeof download.tabId === "number" ? download.tabId : initialTabId,
      });
    } catch {
      // Download telemetry must never interrupt response capture.
    }
  };

  chromeApi.debugger.onEvent.addListener(onDebuggerEvent);
  chromeApi.tabs.onCreated?.addListener(onTabCreated);
  chromeApi.tabs.onUpdated?.addListener(onTabUpdated);
  chromeApi.downloads?.onCreated?.addListener(onDownloadCreated);
  const timeoutTimer = setTimeout(() => {
    finish(lastReadError || new Error("浏览器已打开 PDF，但未能读取完整导航响应"));
  }, timeoutMs);

  try {
    try {
      const initialTab = await chromeApi.tabs.get?.(initialTabId);
      initialPageUrl = initialTab?.url || initialTab?.pendingUrl || null;
    } catch {
      // A disappearing tab is reported by debugger.attach below.
    }
    await attachTab(initialTabId);
  } catch (error) {
    finish(new Error(`无法监听出版社标签页：${error instanceof Error ? error.message : String(error)}`));
  }

  return {
    async ensureTab(tabId) {
      await attachTab(tabId);
    },
    waitForPdf() {
      return captured;
    },
    async resultWithin(milliseconds) {
      return Promise.race([
        captured.then((value) => value),
        new Promise((resolve) => setTimeout(() => resolve(null), milliseconds)),
      ]);
    },
    async close(reason = null) {
      if (closed) return;
      closed = true;
      if (!settled) finish(reason || new Error("PDF 导航响应捕获已停止"));
      chromeApi.debugger.onEvent.removeListener(onDebuggerEvent);
      chromeApi.tabs.onCreated?.removeListener(onTabCreated);
      chromeApi.tabs.onUpdated?.removeListener(onTabUpdated);
      chromeApi.downloads?.onCreated?.removeListener(onDownloadCreated);
      await Promise.allSettled(Array.from(attachedTabs, (tabId) => chromeApi.debugger.detach({ tabId })));
    },
  };
}
