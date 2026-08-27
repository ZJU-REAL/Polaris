import { DEFAULT_MAX_PDF_BYTES } from "./constants.js";
import { hasSensitiveUrlParameters, parseSafeHttpUrl } from "./url-security.js";

const WINDOWS_RESERVED = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i;

export function cleanText(value, maxLength = 1000) {
  return String(value ?? "").replace(/\s+/g, " ").trim().slice(0, maxLength);
}

export function canonicalDoi(value) {
  return cleanText(value, 512)
    .replace(/^doi\s*:\s*/i, "")
    .replace(/^https?:\/\/(?:dx\.)?doi\.org\//i, "")
    .replace(/[\s.,;]+$/g, "")
    .toLowerCase();
}

export function canonicalTitle(value) {
  return cleanText(value, 2000).toLocaleLowerCase().replace(/[^a-z0-9\u4e00-\u9fff]+/g, "");
}

export function stableHash(value) {
  let hash = 0x811c9dc5;
  for (const char of String(value || "")) {
    hash ^= char.codePointAt(0);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}

export function sanitizeFilename(value, maxLength = 96) {
  let text = cleanText(value, 500)
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, "_")
    .replace(/\.{2,}/g, ".")
    .replace(/[. ]+$/g, "")
    .replace(/_+/g, "_")
    .slice(0, Math.max(8, maxLength));
  if (!text) text = "paper";
  if (WINDOWS_RESERVED.test(text)) text = `_${text}`;
  return text;
}

export function createTaskCode(source = {}, now = new Date()) {
  const sourceId = cleanText(source.searchId || source.runId, 80);
  if (sourceId) {
    const prefix = source.area === "literature-search"
      ? "ls"
      : source.area === "exclusive-review" ? "ex" : source.area === "daily-review" ? "rv" : "";
    return sanitizeFilename(prefix ? `${prefix}-${sourceId}` : sourceId, 48);
  }
  const topic = canonicalTitle(source.topic || "literature").slice(0, 24) || "literature";
  const stamp = now.toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
  return `${topic}-${stableHash(`${topic}:${stamp}`)}`.slice(0, 48);
}

export function plannedPdfFilename(template, { taskCode, ordinal, title, doi }) {
  const index = String(Math.max(1, Number(ordinal) || 1)).padStart(3, "0");
  const source = cleanText(template, 160) || "{taskCode}_{index}";
  const includesIndex = source.includes("{index}");
  const rendered = source
    .replaceAll("{taskCode}", taskCode || "task")
    .replaceAll("{index}", index)
    .replaceAll("{title}", title || "paper")
    .replaceAll("{doi}", doi || "no-doi")
    .replace(/\.pdf$/i, "");
  const uniqueStem = includesIndex ? rendered : `${rendered}_${index}`;
  return `${sanitizeFilename(uniqueStem, 80)}.pdf`;
}

export function normalizePaperRecord(raw, index = 0) {
  const doi = canonicalDoi(raw?.doi);
  const title = cleanText(raw?.title, 1000);
  const idSeed = doi || canonicalTitle(title) || cleanText(raw?.id, 160) || `paper-${index + 1}`;
  const url = parseSafeHttpUrl(raw?.url, { allowRelative: true, allowLocalDevelopment: true });
  const pdfUrl = parseSafeHttpUrl(raw?.pdfUrl, { allowRelative: true, allowLocalDevelopment: true });
  const pdfRemoteUrl = parseSafeHttpUrl(raw?.pdfRemoteUrl, { allowRelative: true, allowLocalDevelopment: true });
  return {
    id: `paper-${stableHash(idSeed)}`,
    yfrPaperId: cleanText(raw?.id, 160) || null,
    doi: doi || null,
    title: title || doi || `Untitled paper ${index + 1}`,
    authors: Array.isArray(raw?.authors) ? raw.authors.slice(0, 50).map((item) => cleanText(item, 240)).filter(Boolean) : [],
    year: Number.isInteger(raw?.year) ? raw.year : null,
    venue: cleanText(raw?.venue, 500) || null,
    publisher: cleanText(raw?.publisher, 500) || null,
    articleUrl: url,
    sources: Array.isArray(raw?.sources) ? raw.sources.slice(0, 24).map((item) => cleanText(item, 160)).filter(Boolean) : [],
    identifiers: {},
    polarisTarget: raw?.polarisTarget && typeof raw.polarisTarget === "object"
      ? { ...raw.polarisTarget }
      : null,
    candidates: [
      ...(Array.isArray(raw?.candidates) ? raw.candidates.map((candidate) => ({ ...candidate })) : []),
      ...(pdfUrl ? [{
        url: pdfUrl,
        source: "yfr",
        kind: raw?.pdfAvailable ? "open-access" : "unknown",
        sessionBound: hasSensitiveUrlParameters(pdfUrl),
      }] : []),
      ...(pdfRemoteUrl && pdfRemoteUrl !== pdfUrl ? [{ url: pdfRemoteUrl, source: "yfr", kind: "unknown", sessionBound: true }] : []),
    ],
  };
}

export function dedupePaperRecords(records) {
  const byKey = new Map();
  for (const [index, raw] of records.entries()) {
    const paper = normalizePaperRecord(raw, index);
    const key = paper.doi ? `doi:${paper.doi}` : `title:${canonicalTitle(paper.title)}`;
    const existing = byKey.get(key);
    if (!existing) {
      byKey.set(key, paper);
      continue;
    }
    existing.sources = Array.from(new Set([...existing.sources, ...paper.sources]));
    existing.candidates = Array.from(
      new Map([...existing.candidates, ...paper.candidates].map((candidate) => [candidate.url, candidate])).values(),
    );
    existing.articleUrl ||= paper.articleUrl;
    existing.publisher ||= paper.publisher;
    existing.venue ||= paper.venue;
  }
  return Array.from(byKey.values());
}

export function normalizeMaxPdfBytes(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return DEFAULT_MAX_PDF_BYTES;
  return Math.max(1024 * 1024, Math.min(DEFAULT_MAX_PDF_BYTES, Math.floor(numeric)));
}
