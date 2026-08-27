import { ITEM_STATE } from "../shared/constants.js";
import { parseSafeHttpUrl } from "../shared/url-security.js";

const SCIENCEDIRECT_PDF_HOST = "pdf.sciencedirectassets.com";
const ASSET_SOURCE_DETAIL = "sciencedirect-pdf-asset";
const CHROMIUM_PDF_VIEWER_IDS = new Set([
  "mhjfbmdgcfjbbpaeojofohoefgiehjai",
]);

function normalizePii(value) {
  return String(value || "").trim().toUpperCase();
}

export function scienceDirectArticleIdentity(value) {
  try {
    const safeUrl = parseSafeHttpUrl(value);
    if (!safeUrl) return null;
    const url = new URL(safeUrl);
    const hostname = url.hostname.toLowerCase();
    if (hostname === "linkinghub.elsevier.com") {
      const redirected = url.searchParams.get("Redirect");
      if (redirected) {
        const redirectedIdentity = scienceDirectArticleIdentity(redirected);
        if (redirectedIdentity) return redirectedIdentity;
      }
      const pii = normalizePii(url.pathname.match(/\/pii\/([a-z0-9]+)/i)?.[1]);
      return pii ? { pii, articleUrl: `https://www.sciencedirect.com/science/article/pii/${pii}` } : null;
    }
    if (hostname !== "sciencedirect.com" && !hostname.endsWith(".sciencedirect.com")) return null;
    const pii = normalizePii(url.pathname.match(/\/science\/article\/pii\/([a-z0-9]+)/i)?.[1]);
    return pii ? { pii, articleUrl: `https://www.sciencedirect.com/science/article/pii/${pii}` } : null;
  } catch {
    return null;
  }
}

function signedExpiry(url) {
  const value = url.searchParams.get("X-Amz-Date");
  const lifetime = Number(url.searchParams.get("X-Amz-Expires"));
  const match = String(value || "").match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/);
  if (!match || !Number.isFinite(lifetime) || lifetime <= 0) return null;
  const [, year, month, day, hour, minute, second] = match;
  const issuedAt = Date.UTC(Number(year), Number(month) - 1, Number(day), Number(hour), Number(minute), Number(second));
  return new Date(issuedAt + Math.min(lifetime, 86400) * 1000).toISOString();
}

function unwrapChromiumPdfViewer(value) {
  try {
    const wrapper = new URL(value);
    const knownChromeViewer = wrapper.protocol === "chrome-extension:"
      && CHROMIUM_PDF_VIEWER_IDS.has(wrapper.hostname);
    const knownEdgeViewer = wrapper.protocol === "edge:" && wrapper.hostname === "pdf-viewer";
    if (!knownChromeViewer && !knownEdgeViewer) return null;
    return parseSafeHttpUrl(wrapper.searchParams.get("file") || wrapper.searchParams.get("src"));
  } catch {
    return null;
  }
}

export function parseScienceDirectPdfAsset(value, now = new Date()) {
  const safeUrl = parseSafeHttpUrl(value) || unwrapChromiumPdfViewer(value);
  if (!safeUrl) return null;
  const url = new URL(safeUrl);
  if (url.hostname !== SCIENCEDIRECT_PDF_HOST || !/\.pdf$/i.test(url.pathname)) return null;
  const pii = normalizePii(
    url.searchParams.get("pii")
      || url.pathname.match(/1-s2\.0-([a-z0-9]+)/i)?.[1],
  );
  const expiresAt = signedExpiry(url);
  return {
    url: safeUrl,
    pii: pii || null,
    expiresAt,
    expired: Boolean(expiresAt && Date.parse(expiresAt) <= now.getTime()),
  };
}

export function scienceDirectAssetUsable(asset, now = new Date(), minimumRemainingMs = 15000) {
  if (!asset || asset.expired) return false;
  if (!asset.expiresAt) return true;
  return Date.parse(asset.expiresAt) - now.getTime() > minimumRemainingMs;
}

export function expectedScienceDirectPii(item) {
  return normalizePii(item?.identifiers?.pii || scienceDirectArticleIdentity(item?.articleUrl)?.pii) || null;
}

export function findScienceDirectPdfTab(item, tabs, { preferredTabId = null, now = new Date() } = {}) {
  const expectedPii = expectedScienceDirectPii(item);
  const matches = (Array.isArray(tabs) ? tabs : []).flatMap((tab) => {
    const asset = parseScienceDirectPdfAsset(tab?.pendingUrl || tab?.url, now)
      || parseScienceDirectPdfAsset(tab?.url, now);
    if (!asset || typeof tab?.id !== "number") return [];
    if (expectedPii && asset.pii !== expectedPii) return [];
    if (!expectedPii && tab.id !== preferredTabId) return [];
    return [{ tab, asset }];
  });
  return matches.sort((left, right) => {
    if (left.asset.expired !== right.asset.expired) return left.asset.expired ? 1 : -1;
    if ((left.tab.id === preferredTabId) !== (right.tab.id === preferredTabId)) {
      return left.tab.id === preferredTabId ? -1 : 1;
    }
    return Number(right.tab.lastAccessed || 0) - Number(left.tab.lastAccessed || 0);
  })[0] || null;
}

export function registerScienceDirectPdfAsset(item, match, now = new Date()) {
  if (!match?.asset?.url || typeof match?.tab?.id !== "number") throw new Error("ScienceDirect PDF 标签页无效");
  const candidates = (item.candidates || []).filter((candidate) => candidate.sourceDetail !== ASSET_SOURCE_DETAIL);
  candidates.push({
    url: match.asset.url,
    source: "publisher-tab",
    sourceDetail: ASSET_SOURCE_DETAIL,
    kind: "institutional",
    discoveredAt: now.toISOString(),
    expiresAt: match.asset.expiresAt,
    sessionBound: true,
    articleUrl: item.articleUrl || null,
  });
  return {
    ...item,
    candidates,
    state: ITEM_STATE.CANDIDATE_REGISTERED,
    manualTabId: match.tab.id,
    articleTabId: item.articleTabId || item.manualTabId || null,
    identifiers: {
      ...(item.identifiers || {}),
      ...(match.asset.pii ? { pii: match.asset.pii } : {}),
    },
    statusReason: "已捕获当前标签页的 ScienceDirect PDF，正在校验文件签名",
    updatedAt: now.toISOString(),
  };
}

export const SCIENCEDIRECT_ASSET_SOURCE_DETAIL = ASSET_SOURCE_DETAIL;
