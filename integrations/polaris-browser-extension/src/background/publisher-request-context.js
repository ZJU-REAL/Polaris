import { parseSafeHttpUrl } from "../shared/url-security.js";

const SCIENCEDIRECT_PDF_HOST = "pdf.sciencedirectassets.com";
const SCIENCEDIRECT_HOST = "www.sciencedirect.com";
const SCIENCEDIRECT_CONTEXT_RULE_ID = 9040101;

let contextQueue = Promise.resolve();

function escapeRegex(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function scienceDirectPdfUrl(value) {
  const safe = parseSafeHttpUrl(value);
  if (!safe) throw new Error("ScienceDirect PDF 地址无效");
  const url = new URL(safe);
  if (url.protocol !== "https:" || url.hostname !== SCIENCEDIRECT_PDF_HOST || !/\.pdf$/i.test(url.pathname)) {
    throw new Error("ScienceDirect PDF 地址不属于受支持的资源域名");
  }
  return url;
}

function scienceDirectReferer(value) {
  const fallback = `https://${SCIENCEDIRECT_HOST}/`;
  const safe = parseSafeHttpUrl(value || fallback);
  if (!safe) return fallback;
  const url = new URL(safe);
  const trustedHost = url.hostname === "sciencedirect.com" || url.hostname.endsWith(".sciencedirect.com");
  if (url.protocol !== "https:" || !trustedHost) return fallback;
  url.username = "";
  url.password = "";
  url.search = "";
  url.hash = "";
  return url.toString();
}

export function buildScienceDirectRequestContextRule({ assetUrl, articleUrl, extensionId }) {
  const asset = scienceDirectPdfUrl(assetUrl);
  const referer = scienceDirectReferer(articleUrl);
  const initiator = String(extensionId || "").trim();
  if (!/^[a-p]{32}$/.test(initiator)) throw new Error("扩展标识无效，无法建立 ScienceDirect 请求上下文");

  return {
    id: SCIENCEDIRECT_CONTEXT_RULE_ID,
    priority: 1,
    action: {
      type: "modifyHeaders",
      requestHeaders: [
        { header: "Referer", operation: "set", value: referer },
        { header: "Origin", operation: "remove" },
      ],
    },
    condition: {
      regexFilter: `^https://${escapeRegex(SCIENCEDIRECT_PDF_HOST)}${escapeRegex(asset.pathname)}(?:\\?.*)?$`,
      requestDomains: [SCIENCEDIRECT_PDF_HOST],
      initiatorDomains: [initiator],
      resourceTypes: ["xmlhttprequest", "other"],
    },
  };
}

function requireDnr(chromeApi) {
  const api = chromeApi?.declarativeNetRequest;
  if (!api?.updateSessionRules) {
    throw new Error("扩展缺少 ScienceDirect 请求上下文权限，请在扩展管理页重新加载新版扩展");
  }
  return api;
}

function serializeContextOperation(operation) {
  const next = contextQueue.then(operation, operation);
  contextQueue = next.catch(() => undefined);
  return next;
}

export async function withScienceDirectRequestContext(chromeApi, options, operation) {
  if (typeof operation !== "function") throw new Error("ScienceDirect 缓存操作无效");
  return serializeContextOperation(async () => {
    const api = requireDnr(chromeApi);
    const rule = buildScienceDirectRequestContextRule({
      ...options,
      extensionId: chromeApi?.runtime?.id,
    });
    await api.updateSessionRules({
      removeRuleIds: [SCIENCEDIRECT_CONTEXT_RULE_ID],
      addRules: [rule],
    });
    try {
      return await operation();
    } finally {
      await api.updateSessionRules({ removeRuleIds: [SCIENCEDIRECT_CONTEXT_RULE_ID] });
    }
  });
}

export async function clearPublisherRequestContextRules(chromeApi) {
  return serializeContextOperation(async () => {
    const api = chromeApi?.declarativeNetRequest;
    if (!api?.updateSessionRules) return;
    await api.updateSessionRules({ removeRuleIds: [SCIENCEDIRECT_CONTEXT_RULE_ID] });
  });
}

export const PUBLISHER_REQUEST_CONTEXT_RULE_ID = SCIENCEDIRECT_CONTEXT_RULE_ID;
