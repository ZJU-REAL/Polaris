import { ITEM_STATE } from "../shared/constants.js";
import { hasSensitiveUrlParameters, parseSafeHttpUrl } from "../shared/url-security.js";
import { applyPublisherSnapshot } from "./adapters.js";
import { isPlausiblePublisherCandidate, normalizePageSnapshot } from "./page-snapshot.js";
import { SCIENCEDIRECT_ASSET_SOURCE_DETAIL } from "./sciencedirect-asset.js";
import { derivePublisherPdfCandidates } from "./pdf-rules.js";

function mergeCandidates(existing, incoming) {
  return Array.from(new Map([...(existing || []), ...(incoming || [])]
    .map((candidate) => [candidate.url, candidate])).values());
}

export function registerPublisherRuleCandidates(item, articleUrl = item?.articleUrl, now = new Date()) {
  const incoming = derivePublisherPdfCandidates({
    articleUrl,
    doi: item?.doi,
    identifiers: item?.identifiers,
  }).map((candidate) => ({
    ...candidate,
    articleUrl,
    discoveredAt: now.toISOString(),
    expiresAt: null,
  }));
  if (!incoming.length) return item;
  return {
    ...item,
    candidates: mergeCandidates(item.candidates, incoming),
    state: item.state === ITEM_STATE.RESOLVING ? ITEM_STATE.CANDIDATE_REGISTERED : item.state,
    updatedAt: now.toISOString(),
  };
}

export function registerMetadataCandidates(item, metadata, now = new Date()) {
  if (!metadata) return item;
  const candidates = [...(item.candidates || [])];
  const pdfUrl = parseSafeHttpUrl(metadata.pdfUrl);
  if (pdfUrl && !candidates.some((candidate) => candidate.url === pdfUrl)) {
    const ephemeralNavigationOnly = hasSensitiveUrlParameters(pdfUrl);
    candidates.push({
      url: pdfUrl,
      source: metadata.source || "metadata",
      kind: metadata.source === "openalex" ? "open-access" : "unknown",
      discoveredAt: now.toISOString(),
      expiresAt: null,
      sessionBound: metadata.source !== "openalex",
      browserNavigationPreferred: metadata.source !== "openalex",
      sessionNavigationOnly: ephemeralNavigationOnly,
      ephemeralNavigationOnly,
      retriableAfterAccess: true,
      articleUrl: metadata.articleUrl || item.articleUrl || null,
    });
  }
  const currentArticleUrl = (() => {
    try {
      const host = new URL(item.articleUrl).hostname.toLowerCase();
      return host === "doi.org" || host === "dx.doi.org" ? null : item.articleUrl;
    } catch {
      return item.articleUrl;
    }
  })();
  const articleUrl = currentArticleUrl || metadata.articleUrl || item.articleUrl;
  return registerPublisherRuleCandidates({
    ...item,
    title: item.title || metadata.title,
    publisher: item.publisher || metadata.publisher,
    articleUrl,
    candidates,
    state: candidates.length ? ITEM_STATE.CANDIDATE_REGISTERED : item.state,
    updatedAt: now.toISOString(),
  }, articleUrl, now);
}

export function registerPublisherSnapshot(item, rawSnapshot) {
  const snapshot = normalizePageSnapshot(rawSnapshot);
  return registerPublisherRuleCandidates(applyPublisherSnapshot(item, snapshot), snapshot.canonicalUrl);
}

export function bestCandidate(item) {
  const candidates = item.candidates || [];
  const failed = new Set(item.failedCandidateUrls || []);
  return candidates.filter((candidate) => !candidate.requiresFreshDiscovery
    && !failed.has(candidate.url) && isPlausiblePublisherCandidate(candidate)).sort((left, right) => {
    const sourceScore = (candidate) => candidate.preflight?.status === "verified" ? 5
      : candidate.sourceDetail === SCIENCEDIRECT_ASSET_SOURCE_DETAIL ? 4.8
        : candidate.sourceDetail === "ojs-article-download" ? 4.75
        : candidate.sourceDetail === "quantum-pdf" ? 4.75
          : candidate.sourceDetail === "wiley-pdfdirect" ? 4.75
            : candidate.sourceDetail === "ieee-get-pdf" ? 4.75
        : candidate.sourceDetail === "sciencedirect-view-pdf" ? 4.5
        : candidate.sourceDetail === "citation_pdf_url" ? 4.7
          : candidate.sourceDetail === "article-pdf-link" ? 4.6
            : candidate.sourceDetail === "pdf-action" ? 4.5
              : candidate.sourceDetail === "embedded-pdf" ? 4.4
                : candidate.source === "publisher-rule" && candidate.kind === "open-access" ? 4.3
                  : candidate.source === "publisher-rule" ? 4.2
        : candidate.source === "yfr" && candidate.kind === "open-access" ? 4
          : candidate.kind === "open-access" ? 3
            : candidate.source === "publisher-page" ? 2 : 1;
    return sourceScore(right) - sourceScore(left);
  })[0] || null;
}
