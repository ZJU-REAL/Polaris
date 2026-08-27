import { cleanText } from "../shared/normalization.js";
import { parseSafeHttpUrl } from "../shared/url-security.js";
import { isWceePdfDownloadUrl } from "./pdf-rules.js";

export function hasPdfResourceEvidence(value, label = "") {
  const safe = parseSafeHttpUrl(value);
  if (!safe) return false;
  const url = new URL(safe);
  const path = `${url.pathname}${url.search}`;
  return isWceePdfDownloadUrl(safe)
    || /(?:^|\b)(?:application\/)?pdf(?:\b|$)/i.test(label)
    || /(?:\.pdf(?:$|[/?#])|\/(?:pdf|epdf|pdfft|pdfdirect)\/?(?:$|[?#])|\/stamp\/stamp\.jsp(?:$|[?#]))/i.test(path);
}

export function isPlausiblePublisherCandidate(candidate) {
  const source = cleanText(candidate?.sourceDetail || candidate?.source, 80);
  if (!["embedded-pdf", "document-link"].includes(source)) return Boolean(parseSafeHttpUrl(candidate?.url));
  return hasPdfResourceEvidence(candidate?.url, candidate?.label);
}

export function normalizePageSnapshot(raw) {
  const pageUrl = parseSafeHttpUrl(raw?.pageUrl);
  const canonicalUrl = parseSafeHttpUrl(raw?.canonicalUrl) || pageUrl;
  if (!pageUrl) throw new Error("出版社页面地址无效");
  return {
    pageUrl,
    canonicalUrl,
    hostname: new URL(pageUrl).hostname.toLowerCase(),
    title: cleanText(raw?.title, 1000),
    doi: cleanText(raw?.doi, 512),
    publisher: cleanText(raw?.publisher, 500),
    pii: cleanText(raw?.pii, 160),
    ieeeDocumentNumber: cleanText(raw?.ieeeDocumentNumber, 160),
    articleId: cleanText(raw?.articleId, 160),
    candidates: Array.isArray(raw?.candidates)
      ? raw.candidates.slice(0, 30).map((candidate) => ({
          url: parseSafeHttpUrl(candidate?.url),
          source: cleanText(candidate?.source, 80) || "publisher-page",
          label: cleanText(candidate?.label, 240),
          actionId: cleanText(candidate?.actionId, 120) || null,
        })).filter((candidate) => candidate.url && isPlausiblePublisherCandidate(candidate))
      : [],
    access: {
      captcha: Boolean(raw?.access?.captcha),
      loginRequired: Boolean(raw?.access?.loginRequired),
      noEntitlement: Boolean(raw?.access?.noEntitlement),
    },
    capturedAt: cleanText(raw?.capturedAt, 80) || new Date().toISOString(),
  };
}
