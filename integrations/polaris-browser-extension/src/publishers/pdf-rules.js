import { canonicalDoi } from "../shared/normalization.js";
import { hasSensitiveUrlParameters, parseSafeHttpUrl } from "../shared/url-security.js";

function safeUrl(value) {
  const parsed = parseSafeHttpUrl(value);
  if (!parsed) return null;
  try {
    return new URL(parsed);
  } catch {
    return null;
  }
}

export function deriveOjsPdfDownloadUrl(value) {
  const url = safeUrl(value);
  if (!url) return null;
  const match = url.pathname.match(/^(.*\/article)\/view\/([^/]+)\/([^/]+)\/?$/i);
  if (!match) return null;
  url.pathname = `${match[1]}/download/${match[2]}/${match[3]}/`;
  return url.toString();
}

export function isWceePdfDownloadUrl(value) {
  const url = safeUrl(value);
  if (!url) return false;
  const host = url.hostname.toLowerCase();
  if (host !== "proceedings-wcee.org" && !host.endsWith(".proceedings-wcee.org")) return false;
  if (!/^\/downloadFile\/[^/]+\/?$/i.test(url.pathname)) return false;
  const file = String(url.searchParams.get("file") || "").trim();
  const category = String(url.searchParams.get("category") || "").trim();
  return /^[a-z0-9+/=_-]{4,512}$/i.test(file)
    && /^[a-z0-9._-]{2,80}$/i.test(category)
    && url.searchParams.get("mode") === "download";
}

function candidate(url, rule, {
  openAccess = false,
  browserNavigationPreferred = false,
  navigationUrl = null,
  allowUnboundDocument = false,
  sessionNavigationOnly = false,
  navigationTimeoutMs = null,
  expectedQueryParams = null,
  directReader = false,
  allowUnboundPdfResponse = false,
  waitThroughAccessChallenge = false,
  ephemeralNavigationOnly = false,
} = {}) {
  const parsed = safeUrl(url);
  if (!parsed) return null;
  const navigation = navigationUrl ? safeUrl(navigationUrl) : null;
  const ephemeral = ephemeralNavigationOnly || hasSensitiveUrlParameters(parsed.toString());
  return {
    url: parsed.toString(),
    source: "publisher-rule",
    sourceDetail: rule,
    kind: openAccess ? "open-access" : "institutional",
    sessionBound: !openAccess,
    browserNavigationPreferred,
    navigationUrl: navigation?.toString() || null,
    allowUnboundDocument,
    sessionNavigationOnly: sessionNavigationOnly || ephemeral,
    navigationTimeoutMs,
    expectedQueryParams,
    directReader,
    allowUnboundPdfResponse,
    waitThroughAccessChallenge,
    ephemeralNavigationOnly: ephemeral,
    retriableAfterAccess: true,
  };
}

function ieeeArticleNumber(url, identifiers = {}) {
  return String(identifiers.ieeeDocumentNumber || url.pathname.match(/\/document\/(\d+)/i)?.[1] || "").trim();
}

function wileyDoi(url, doi) {
  const fromPath = url.pathname.match(/^\/doi\/(?:abs\/|full\/|epdf\/|pdfdirect\/)?(.+?)\/?$/i)?.[1] || "";
  try {
    return canonicalDoi(doi) || canonicalDoi(decodeURIComponent(fromPath));
  } catch {
    return canonicalDoi(doi);
  }
}

export function derivePublisherPdfCandidates({ articleUrl, doi, identifiers = {} } = {}) {
  const url = safeUrl(articleUrl);
  if (!url) return [];
  const host = url.hostname.toLowerCase();
  const normalizedDoi = canonicalDoi(doi);
  const candidates = [];

  if (isWceePdfDownloadUrl(url)) {
    candidates.push(candidate(url, "wcee-download-file", {
      openAccess: true,
      browserNavigationPreferred: true,
      allowUnboundDocument: true,
      allowUnboundPdfResponse: true,
      navigationTimeoutMs: 120000,
    }));
  }

  const ojsDownloadUrl = deriveOjsPdfDownloadUrl(url);
  if (ojsDownloadUrl) {
    candidates.push(candidate(ojsDownloadUrl, "ojs-article-download", {
      openAccess: true,
      browserNavigationPreferred: true,
      navigationTimeoutMs: 60000,
    }));
  }

  if (/^watermark\d*\.silverchair\.com$/i.test(host) && /\.pdf$/i.test(url.pathname)) {
    candidates.push(candidate(url, "silverchair-signed-reader", {
      browserNavigationPreferred: true,
      sessionNavigationOnly: true,
      directReader: true,
      navigationTimeoutMs: 120000,
    }));
  }

  if (host === "link.aps.org" && /^\/accepted\//i.test(url.pathname)) {
    candidates.push(candidate(url, "aps-accepted-reader", {
      browserNavigationPreferred: true,
      sessionNavigationOnly: true,
      directReader: true,
      navigationTimeoutMs: 90000,
    }));
  }

  if ((host === "opg.optica.org" || host.endsWith(".opg.optica.org"))
    && /^\/[^/]+\/fulltext\.cfm$/i.test(url.pathname)) {
    const publicationUri = String(url.searchParams.get("uri") || "").trim();
    if (/^[a-z0-9][a-z0-9._-]{2,159}$/i.test(publicationUri)) {
      const pdf = new URL(url.pathname.replace(/fulltext\.cfm$/i, "viewmedia.cfm"), url.origin);
      pdf.searchParams.set("uri", publicationUri);
      pdf.searchParams.set("seq", "0");
      candidates.push(candidate(pdf, "optica-viewmedia", {
        browserNavigationPreferred: true,
        sessionNavigationOnly: true,
        directReader: true,
        allowUnboundPdfResponse: true,
        waitThroughAccessChallenge: true,
        navigationTimeoutMs: 120000,
      }));
    }
  }

  if ((host === "opticsjournal.net" || host.endsWith(".opticsjournal.net"))
    && /^\/Articles\/GetArticlePDF\/[a-z0-9_-]+\/?$/i.test(url.pathname)) {
    candidates.push(candidate(url, "opticsjournal-reader", {
      browserNavigationPreferred: true,
      sessionNavigationOnly: true,
      directReader: true,
      allowUnboundPdfResponse: true,
      waitThroughAccessChallenge: true,
      navigationTimeoutMs: 120000,
    }));
  }

  if (/\.pdf$/i.test(url.pathname) && hasSensitiveUrlParameters(url.toString()) && !candidates.length) {
    candidates.push(candidate(url, "signed-direct-reader", {
      browserNavigationPreferred: true,
      sessionNavigationOnly: true,
      directReader: true,
      navigationTimeoutMs: 120000,
    }));
  }

  if ((host === "quantum-journal.org" || host.endsWith(".quantum-journal.org"))
    && /^\/papers\/[^/]+\/?$/i.test(url.pathname)) {
    candidates.push(candidate(new URL(`${url.pathname.replace(/\/?$/, "/")}pdf/`, url.origin), "quantum-pdf", { openAccess: true }));
  }

  if ((host === "nature.com" || host.endsWith(".nature.com"))
    && /^\/articles\/[^/]+\/?$/i.test(url.pathname)) {
    candidates.push(candidate(new URL(`${url.pathname.replace(/\/$/, "")}.pdf`, url.origin), "nature-article-pdf", { browserNavigationPreferred: true }));
  }

  if ((host === "onlinelibrary.wiley.com" || host.endsWith(".onlinelibrary.wiley.com"))) {
    const publicationDoi = wileyDoi(url, normalizedDoi);
    if (publicationDoi) {
      const pdf = new URL(`/doi/pdfdirect/${publicationDoi}`, url.origin);
      pdf.searchParams.set("download", "true");
      const navigation = new URL(`/doi/epdf/${publicationDoi}`, url.origin);
      candidates.push(candidate(pdf, "wiley-pdfdirect", {
        browserNavigationPreferred: true,
        navigationUrl: navigation,
        allowUnboundDocument: true,
        sessionNavigationOnly: true,
        navigationTimeoutMs: 60000,
      }));
    }
  }

  if ((host === "iopscience.iop.org" || host.endsWith(".iopscience.iop.org"))
    && /^\/article\//i.test(url.pathname) && !/\/pdf\/?$/i.test(url.pathname)) {
    candidates.push(candidate(new URL(`${url.pathname.replace(/\/$/, "")}/pdf`, url.origin), "iop-pdf", { browserNavigationPreferred: true }));
  }

  if ((host === "journals.aps.org" || host.endsWith(".journals.aps.org"))
    && /^\/[^/]+\/(?:abstract|article)\//i.test(url.pathname)) {
    candidates.push(candidate(new URL(url.pathname.replace(/^\/(.+?)\/(?:abstract|article)\//i, "/$1/pdf/"), url.origin), "aps-pdf", { browserNavigationPreferred: true }));
  }

  if (host === "ieeexplore.ieee.org" || host.endsWith(".ieeexplore.ieee.org")) {
    const articleNumber = ieeeArticleNumber(url, identifiers);
    if (articleNumber) {
      const reader = new URL("/stamp/stamp.jsp", url.origin);
      reader.searchParams.set("tp", "");
      reader.searchParams.set("arnumber", articleNumber);
      const referral = new URL(reader);
      referral.searchParams.set("tag", "1");
      const pdf = new URL("/stampPDF/getPDF.jsp", url.origin);
      pdf.searchParams.set("tp", "");
      pdf.searchParams.set("arnumber", articleNumber);
      pdf.searchParams.set("ref", btoa(referral.toString()));
      candidates.push(candidate(pdf, "ieee-get-pdf", {
        browserNavigationPreferred: true,
        navigationUrl: reader,
        sessionNavigationOnly: true,
        navigationTimeoutMs: 60000,
        expectedQueryParams: { arnumber: articleNumber },
      }));
    }
  }

  if ((host === "arxiv.org" || host.endsWith(".arxiv.org")) && /^\/abs\//i.test(url.pathname)) {
    candidates.push(candidate(new URL(url.pathname.replace(/^\/abs\//i, "/pdf/"), url.origin), "arxiv-pdf", { openAccess: true }));
  }

  if ((host === "frontiersin.org" || host.endsWith(".frontiersin.org"))
    && /\/articles\/[^/]+\/(?:full|abstract)\/?$/i.test(url.pathname)) {
    candidates.push(candidate(new URL(url.pathname.replace(/\/(?:full|abstract)\/?$/i, "/pdf"), url.origin), "frontiers-pdf", { openAccess: true }));
  }

  if ((host === "pubs.acs.org" || host.endsWith(".pubs.acs.org")) && normalizedDoi
    && /^\/doi\/(?:abs|full|epdf)\//i.test(url.pathname)) {
    candidates.push(candidate(new URL(`/doi/pdf/${normalizedDoi}`, url.origin), "acs-pdf", { browserNavigationPreferred: true }));
  }

  if ((host === "science.org" || host.endsWith(".science.org")) && normalizedDoi
    && /^\/doi\/(?!epdf\/)/i.test(url.pathname)) {
    candidates.push(candidate(new URL(`/doi/epdf/${normalizedDoi}`, url.origin), "science-epdf", { browserNavigationPreferred: true }));
  }

  return candidates.filter(Boolean);
}

export function hasPreferredBrowserNavigationCandidate(item) {
  return (item?.candidates || []).some((entry) => entry.browserNavigationPreferred);
}
