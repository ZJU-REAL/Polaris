import { ITEM_STATE, KNOWN_PUBLISHERS } from "../shared/constants.js";
import { canonicalDoi, cleanText } from "../shared/normalization.js";
import { hasSensitiveUrlParameters } from "../shared/url-security.js";
import { scienceDirectArticleIdentity } from "./sciencedirect-asset.js";
import { deriveOjsPdfDownloadUrl, isWceePdfDownloadUrl } from "./pdf-rules.js";

function hostMatches(hostname, suffix) {
  return hostname === suffix || hostname.endsWith(`.${suffix}`);
}

function adapterDefinition(snapshot) {
  return KNOWN_PUBLISHERS.find((publisher) => publisher.hosts.some((host) => hostMatches(snapshot.hostname, host)))
    || { id: "generic", label: cleanText(snapshot.publisher, 200) || snapshot.hostname, hosts: [] };
}

function piiFromUrl(value) {
  return String(value || "").match(/\/pii\/([A-Z0-9]+)/i)?.[1] || "";
}

function ieeeNumberFromUrl(value) {
  return String(value || "").match(/(?:document\/|arnumber=)(\d{5,})/i)?.[1] || "";
}

export function identifyPublisher(snapshot) {
  const definition = adapterDefinition(snapshot);
  const scienceDirectIdentity = definition.id === "sciencedirect"
    ? scienceDirectArticleIdentity(snapshot.canonicalUrl) || scienceDirectArticleIdentity(snapshot.pageUrl)
    : null;
  const identifiers = {
    pii: definition.id === "sciencedirect" ? cleanText(snapshot.pii || scienceDirectIdentity?.pii || piiFromUrl(snapshot.canonicalUrl), 160) || null : null,
    ieeeDocumentNumber: definition.id === "ieee" ? cleanText(snapshot.ieeeDocumentNumber || ieeeNumberFromUrl(snapshot.canonicalUrl), 160) || null : null,
    publisherArticleId: cleanText(snapshot.articleId, 160) || null,
  };
  return {
    adapter: definition.id,
    publisherKey: definition.id,
    publisherLabel: definition.label,
    identifiers,
    articleUrl: scienceDirectIdentity?.articleUrl || snapshot.canonicalUrl,
  };
}

export function assessPublisherAccess(snapshot) {
  if (snapshot.access.captcha) return { state: ITEM_STATE.MANUAL_REQUIRED, reason: "出版社要求人工完成人机验证" };
  if (snapshot.access.loginRequired) return { state: ITEM_STATE.LOGIN_REQUIRED, reason: "需要完成机构或个人登录" };
  if (snapshot.access.noEntitlement) return { state: ITEM_STATE.NO_ENTITLEMENT, reason: "当前页面明确显示无全文权限" };
  if (snapshot.candidates.length) return { state: ITEM_STATE.CANDIDATE_REGISTERED, reason: "已发现官方 PDF 入口，正在等待响应校验" };
  return { state: ITEM_STATE.MANUAL_REQUIRED, reason: "页面未提供可确认的 PDF 入口，需要人工检查" };
}

export function buildPublisherCandidates(snapshot, adapter) {
  const publicPublisher = adapter === "quantum";
  return snapshot.candidates.map((candidate) => {
    const ojsDownloadUrl = deriveOjsPdfDownloadUrl(candidate.url);
    const candidateUrl = ojsDownloadUrl || candidate.url;
    const wceeDownload = isWceePdfDownloadUrl(candidateUrl);
    const openAccess = publicPublisher || Boolean(ojsDownloadUrl) || wceeDownload;
    const ephemeralNavigationOnly = hasSensitiveUrlParameters(candidateUrl);
    return ({
    url: candidateUrl,
    source: "publisher-page",
    sourceDetail: ojsDownloadUrl ? "ojs-article-download" : wceeDownload ? "wcee-download-file" : candidate.source,
    kind: openAccess ? "open-access" : "institutional",
    discoveredAt: snapshot.capturedAt,
    expiresAt: null,
    sessionBound: !openAccess,
    articleUrl: snapshot.canonicalUrl,
    adapter,
    retriableAfterAccess: true,
    browserNavigationPreferred: Boolean(ojsDownloadUrl) || Boolean(candidate.actionId) || ephemeralNavigationOnly || (!publicPublisher && adapter !== "sciencedirect"),
    sessionNavigationOnly: ephemeralNavigationOnly,
    ephemeralNavigationOnly,
    actionId: candidate.actionId || null,
    label: candidate.label || "",
    allowUnboundDocument: Boolean(candidate.actionId) || wceeDownload,
    allowUnboundPdfResponse: Boolean(candidate.actionId) || wceeDownload,
    navigationTimeoutMs: candidate.actionId || wceeDownload ? 120000 : null,
    });
  });
}

export function applyPublisherSnapshot(item, snapshot) {
  const identity = identifyPublisher(snapshot);
  const preserveKnownPublisher = identity.publisherKey === "generic" && item.publisherKey;
  const assessment = assessPublisherAccess(snapshot);
  const incoming = buildPublisherCandidates(snapshot, identity.adapter);
  const candidates = Array.from(new Map([...(item.candidates || []), ...incoming].map((candidate) => [candidate.url, candidate])).values());
  return {
    ...item,
    doi: item.doi || canonicalDoi(snapshot.doi) || null,
    title: item.title || snapshot.title,
    publisher: item.publisher || snapshot.publisher || identity.publisherLabel,
    articleUrl: preserveKnownPublisher ? item.articleUrl : identity.articleUrl,
    adapter: preserveKnownPublisher ? item.adapter : identity.adapter,
    publisherKey: preserveKnownPublisher ? item.publisherKey : identity.publisherKey,
    identifiers: { ...(item.identifiers || {}), ...Object.fromEntries(Object.entries(identity.identifiers).filter(([, value]) => value)) },
    candidates,
    state: assessment.state,
    statusReason: assessment.reason,
    lastPageSnapshotAt: snapshot.capturedAt,
    updatedAt: new Date().toISOString(),
  };
}
