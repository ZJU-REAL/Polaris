import { canonicalDoi, cleanText } from "../shared/normalization.js";
import { parseSafeHttpUrl } from "../shared/url-security.js";

const metadataCache = new Map();

export function crossrefMetadata(message) {
  if (!message || typeof message !== "object") return null;
  const pdfLink = Array.isArray(message.link)
    ? message.link.find((link) => /application\/pdf/i.test(link?.["content-type"] || "") && parseSafeHttpUrl(link?.URL))
    : null;
  return {
    doi: canonicalDoi(message.DOI),
    title: cleanText(Array.isArray(message.title) ? message.title[0] : message.title, 1000),
    publisher: cleanText(message.publisher, 500),
    articleUrl: parseSafeHttpUrl(message.URL),
    pdfUrl: parseSafeHttpUrl(pdfLink?.URL),
    source: "crossref",
  };
}

export function openAlexMetadata(work) {
  if (!work || typeof work !== "object") return null;
  const primary = work.primary_location || {};
  const open = work.best_oa_location || {};
  return {
    doi: canonicalDoi(work.doi),
    title: cleanText(work.title || work.display_name, 1000),
    publisher: cleanText(primary.source?.host_organization_name || open.source?.host_organization_name, 500),
    articleUrl: parseSafeHttpUrl(primary.landing_page_url || open.landing_page_url || work.doi),
    pdfUrl: parseSafeHttpUrl(open.pdf_url || primary.pdf_url),
    source: "openalex",
  };
}

function doiResolverUrl(value) {
  try {
    const host = new URL(value).hostname.toLowerCase();
    return host === "doi.org" || host === "dx.doi.org";
  } catch {
    return false;
  }
}

async function fetchJson(url, fetchImpl) {
  const response = await fetchImpl(url, { headers: { Accept: "application/json" }, redirect: "follow" });
  if (!response.ok) throw new Error(`metadata HTTP ${response.status}`);
  return response.json();
}

export async function resolvePaperMetadata(item, fetchImpl = fetch) {
  const doi = canonicalDoi(item.doi);
  if (!doi) return null;
  if (metadataCache.has(doi)) return metadataCache.get(doi);
  let result = null;
  try {
    const payload = await fetchJson(`https://api.crossref.org/works/${encodeURIComponent(doi)}`, fetchImpl);
    result = crossrefMetadata(payload?.message);
  } catch {
    result = null;
  }
  if (!result?.publisher || !result?.articleUrl || !result?.pdfUrl) {
    try {
      const payload = await fetchJson(`https://api.openalex.org/works/https://doi.org/${doi}`, fetchImpl);
      const fallback = openAlexMetadata(payload);
      const existing = Object.fromEntries(Object.entries(result || {}).filter(([, value]) => value));
      result = {
        ...fallback,
        ...existing,
        articleUrl: doiResolverUrl(existing.articleUrl) && fallback?.articleUrl
          ? fallback.articleUrl
          : existing.articleUrl || fallback?.articleUrl,
        pdfUrl: existing.pdfUrl || fallback?.pdfUrl,
      };
    } catch {
      // DOI landing page probing remains available when metadata APIs fail.
    }
  }
  metadataCache.set(doi, result);
  return result;
}

export function clearMetadataCache(doi = null) {
  const key = canonicalDoi(doi);
  if (key) metadataCache.delete(key);
  else metadataCache.clear();
}
