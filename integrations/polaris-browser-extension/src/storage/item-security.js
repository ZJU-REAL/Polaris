import {
  hasSensitiveUrlParameters,
  isTrustedYfrPdfAssetUrl,
  redactSensitiveUrl,
} from "../shared/url-security.js";

function canPersistSensitiveCandidate(candidate, value) {
  if (candidate?.source !== "yfr") return false;
  try {
    const origin = new URL(value).origin;
    return isTrustedYfrPdfAssetUrl(value, origin);
  } catch {
    return false;
  }
}

function sanitizeCandidate(candidate) {
  if (!candidate || typeof candidate !== "object") return candidate;
  const sensitiveUrl = hasSensitiveUrlParameters(candidate.url)
    && !canPersistSensitiveCandidate(candidate, candidate.url);
  const sensitiveNavigationUrl = hasSensitiveUrlParameters(candidate.navigationUrl);
  const sensitiveArticleUrl = hasSensitiveUrlParameters(candidate.articleUrl);
  const ephemeral = Boolean(candidate.ephemeralNavigationOnly || sensitiveUrl || sensitiveNavigationUrl);
  return {
    ...candidate,
    url: sensitiveUrl ? redactSensitiveUrl(candidate.url) : candidate.url,
    navigationUrl: sensitiveNavigationUrl ? redactSensitiveUrl(candidate.navigationUrl) : candidate.navigationUrl,
    articleUrl: sensitiveArticleUrl ? redactSensitiveUrl(candidate.articleUrl) : candidate.articleUrl,
    ...(ephemeral ? {
      ephemeralNavigationOnly: true,
      sessionNavigationOnly: true,
      requiresFreshDiscovery: true,
      signedUrlCleared: true,
    } : {}),
  };
}

export function sanitizeItemForPersistence(item) {
  if (!item || typeof item !== "object") return item;
  const allowedActiveUrl = Array.isArray(item.candidates)
    ? item.candidates.some((candidate) => candidate?.url === item.activeCandidateUrl
      && canPersistSensitiveCandidate(candidate, item.activeCandidateUrl))
    : false;
  const candidates = Array.isArray(item.candidates) ? item.candidates.map(sanitizeCandidate) : item.candidates;
  const activeCandidateUrl = hasSensitiveUrlParameters(item.activeCandidateUrl) && !allowedActiveUrl
    ? null
    : item.activeCandidateUrl;
  return {
    ...item,
    articleUrl: hasSensitiveUrlParameters(item.articleUrl) ? redactSensitiveUrl(item.articleUrl) : item.articleUrl,
    activeCandidateUrl,
    candidates,
  };
}
