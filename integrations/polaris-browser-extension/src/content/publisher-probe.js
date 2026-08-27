(() => {
  const PROBE_MESSAGE = "PROBE_PUBLISHER_PAGE";
  const PREFLIGHT_MESSAGE = "PREFLIGHT_PDF_CANDIDATE";
  const NAVIGATE_PDF_MESSAGE = "NAVIGATE_PUBLISHER_PDF";

  function text(value, max = 2000) {
    return String(value || "").replace(/\s+/g, " ").trim().slice(0, max);
  }

  function absoluteUrl(value) {
    if (typeof value !== "string" || !value.trim()) return null;
    try {
      const url = new URL(value, location.href);
      return ["http:", "https:"].includes(url.protocol) ? url.toString() : null;
    } catch {
      return null;
    }
  }

  function hasPdfResourceEvidence(value, label = "", type = "") {
    const absolute = absoluteUrl(value);
    if (!absolute) return false;
    const url = new URL(absolute);
    const path = `${url.pathname}${url.search}`;
    const wceeDownload = (url.hostname === "proceedings-wcee.org" || url.hostname.endsWith(".proceedings-wcee.org"))
      && /^\/downloadFile\/[^/]+\/?$/i.test(url.pathname)
      && /^[a-z0-9+/=_-]{4,512}$/i.test(String(url.searchParams.get("file") || ""))
      && /^[a-z0-9._-]{2,80}$/i.test(String(url.searchParams.get("category") || ""))
      && url.searchParams.get("mode") === "download";
    return /application\/pdf/i.test(type)
      || wceeDownload
      || /(?:^|\b)pdf(?:\b|$)/i.test(label)
      || /(?:\.pdf(?:$|[/?#])|\/(?:pdf|epdf|pdfft|pdfdirect)\/?(?:$|[?#])|\/stamp\/stamp\.jsp(?:$|[?#]))/i.test(path);
  }

  function isPrimaryPdfLabel(value) {
    const label = text(value, 240).replace(/[‐‑‒–—_-]+/g, " ");
    if (!label) return false;
    return /\b(?:get|view|open|download|read|access)\b[^\n]{0,40}\bpdf\b/i.test(label)
      || /\bfull\s*text\b[^\n]{0,24}\bpdf\b/i.test(label)
      || /\bpdf\b[^\n]{0,40}\b(?:view|open|download|read|full\s*text)\b/i.test(label)
      || /^(?:pdf|全文\s*pdf|pdf\s*全文)$/i.test(label.trim())
      || /(?:获取|查看|打开|阅读|下载)(?:\s*全文)?\s*pdf|(?:全文\s*pdf|pdf\s*全文)(?:\s*(?:查看|打开|阅读|下载))?/i.test(label);
  }

  function meta(names) {
    for (const name of names) {
      const selector = `meta[name="${CSS.escape(name)}"],meta[property="${CSS.escape(name)}"]`;
      const value = document.querySelector(selector)?.getAttribute("content");
      if (value) return text(value, 4096);
    }
    return "";
  }

  function jsonLdCandidates() {
    const urls = [];
    for (const node of document.querySelectorAll('script[type="application/ld+json"]')) {
      try {
        const parsed = JSON.parse(node.textContent || "null");
        const queue = Array.isArray(parsed) ? [...parsed] : [parsed];
        while (queue.length) {
          const value = queue.shift();
          if (!value || typeof value !== "object") continue;
          for (const key of ["contentUrl", "encoding", "associatedMedia", "url"]) {
            const candidate = value[key];
            if (typeof candidate === "string" && /pdf/i.test(candidate)) urls.push(candidate);
            else if (candidate && typeof candidate === "object") queue.push(candidate);
          }
          if (Array.isArray(value["@graph"])) queue.push(...value["@graph"]);
        }
      } catch {
        // Invalid publisher metadata is ignored and never evaluated.
      }
    }
    return urls;
  }

  function collectCandidates() {
    const candidates = [];
    const seen = new Set();
    let actionIndex = 0;
    function add(url, source, label = "", actionId = null) {
      const absolute = absoluteUrl(url);
      const key = `${absolute || ""}\n${actionId || ""}`;
      if (!absolute || seen.has(key)) return;
      seen.add(key);
      candidates.push({ url: absolute, source, label: text(label, 240), actionId });
    }

    function auxiliaryPdf(label, href = "") {
      return /(?:supp(?:lement(?:ary)?)?|supporting(?:[\s_-]+information)?|appendix|poster|correction|corrigendum|erratum|graphical[\s_-]+abstract|toc[\s_-]+graphic|补充|附件)/i
        .test(`${label} ${href}`);
    }

    if (location.hostname === "www.sciencedirect.com" || location.hostname.endsWith(".sciencedirect.com")) {
      for (const anchor of document.querySelectorAll('a[href*="/pdfft"]')) {
        const href = absoluteUrl(anchor.getAttribute("href"));
        if (!href) continue;
        const parsed = new URL(href);
        if (/^\/science\/article\/pii\/[a-z0-9]+\/pdfft\/?$/i.test(parsed.pathname)) {
          add(href, "sciencedirect-view-pdf", anchor.textContent || anchor.getAttribute("aria-label") || "View PDF");
        }
      }
    }
    add(meta(["citation_pdf_url", "wkhealth_pdf_url", "eprints.document_url"]), "citation_pdf_url");
    for (const url of jsonLdCandidates()) add(url, "json-ld");
    for (const link of document.querySelectorAll('link[href][rel="alternate"], link[href][type*="pdf" i]')) {
      const href = link.getAttribute("href");
      const label = link.getAttribute("title") || "";
      const type = link.getAttribute("type") || "";
      if (hasPdfResourceEvidence(href, label, type)) add(href, "document-link", label || type);
    }
    for (const frame of document.querySelectorAll("iframe[src], embed[src]")) {
      const src = frame.getAttribute("src");
      const label = frame.getAttribute("title") || "";
      const type = frame.getAttribute("type") || "";
      const absolute = absoluteUrl(src);
      if (absolute) {
        try {
          const nestedFile = new URL(absolute).searchParams.get("file");
          if (nestedFile) add(nestedFile, "embedded-pdf-file", label || "PDF viewer file");
        } catch {
          // Invalid nested viewer URLs are ignored.
        }
      }
      if (hasPdfResourceEvidence(src, label, type)) add(src, "embedded-pdf", label || type);
    }
    for (const object of document.querySelectorAll("object[data]")) {
      const data = object.getAttribute("data");
      const label = object.getAttribute("title") || "";
      const type = object.getAttribute("type") || "";
      if (hasPdfResourceEvidence(data, label, type)) add(data, "embedded-pdf", label || type);
    }
    for (const anchor of document.querySelectorAll("a[href]")) {
      const href = anchor.getAttribute("href") || "";
      const label = text(anchor.textContent || anchor.getAttribute("aria-label") || anchor.getAttribute("title"), 240);
      const auxiliary = auxiliaryPdf(label, href);
      const explicitPdf = isPrimaryPdfLabel(label);
      if (!auxiliary && hasPdfResourceEvidence(href, label)) add(href, "article-pdf-link", label);
      else if (!auxiliary && explicitPdf) add(href, "article-pdf-link", label);
      else if (!auxiliary && /(?:\.pdf(?:$|[?#])|\/pdf\/?(?:$|[?#]))/i.test(href)) add(href, "pdf-file-link", label);
      if (candidates.length >= 30) break;
    }
    for (const control of document.querySelectorAll('button, [role="button"]')) {
      const label = text(control.textContent || control.getAttribute("aria-label") || control.getAttribute("title"), 240);
      if (!isPrimaryPdfLabel(label)) continue;
      if (auxiliaryPdf(label)) continue;
      const actionId = `yfr-pdf-action-${actionIndex += 1}`;
      control.setAttribute("data-yfr-pdf-action", actionId);
      add(location.href, "pdf-action", label, actionId);
      if (candidates.length >= 30) break;
    }
    return candidates.slice(0, 30);
  }

  function matchingPdfEntry(candidateUrl, actionId = null) {
    if (actionId) {
      const action = document.querySelector(`[data-yfr-pdf-action="${CSS.escape(actionId)}"]`);
      if (action) return { element: action, url: candidateUrl, action: "page-control-click" };
      return null;
    }
    const expected = absoluteUrl(candidateUrl);
    if (expected) {
      for (const anchor of document.querySelectorAll("a[href]")) {
        if (absoluteUrl(anchor.getAttribute("href")) === expected) {
          return { element: null, url: expected, action: "same-tab-navigation" };
        }
      }
    }
    if (!(location.hostname === "www.sciencedirect.com" || location.hostname.endsWith(".sciencedirect.com"))) {
      return expected ? { element: null, url: expected, action: "same-tab-navigation" } : null;
    }
    for (const anchor of document.querySelectorAll('a[href*="/pdfft"]')) {
      const href = absoluteUrl(anchor.getAttribute("href"));
      if (!href) continue;
      const parsed = new URL(href);
      if (/^\/science\/article\/pii\/[a-z0-9]+\/pdfft\/?$/i.test(parsed.pathname)) {
        return { element: null, url: href, action: "same-tab-navigation" };
      }
    }
    return null;
  }

  function collectSnapshot() {
    const bodyText = text(document.body?.innerText, 80000).toLocaleLowerCase();
    const captcha = /captcha|verify you are human|human verification|cloudflare|机器人验证|人机验证|安全验证/.test(bodyText);
    const loginRequired = /sign in via your institution|institutional login|access through your institution|log in to access|通过机构登录|机构登录|登录后访问/.test(bodyText);
    const noEntitlement = /purchase pdf|buy this article|rent this article|access denied|you do not have access|没有访问权限|购买全文|暂无权限/.test(bodyText);
    const currentUrl = location.href;
    return {
      pageUrl: currentUrl,
      canonicalUrl: absoluteUrl(document.querySelector('link[rel="canonical"]')?.getAttribute("href")) || currentUrl,
      hostname: location.hostname.toLowerCase(),
      title: meta(["citation_title", "dc.title", "DC.Title"]) || text(document.title, 1000),
      doi: meta(["citation_doi", "dc.identifier", "DC.Identifier"]).replace(/^doi:\s*/i, ""),
      publisher: meta(["citation_publisher", "dc.publisher", "DC.Publisher"]),
      pii: meta(["citation_pii", "pii"]),
      ieeeDocumentNumber: meta(["citation_arnumber", "arnumber"]),
      articleId: meta(["citation_id", "article_id"]),
      documentReadyState: document.readyState,
      candidates: collectCandidates(),
      access: { captcha, loginRequired, noEntitlement },
      capturedAt: new Date().toISOString(),
    };
  }

  async function responsePrefix(response) {
    if (response.body?.getReader) {
      const reader = response.body.getReader();
      try {
        const { value } = await reader.read();
        return Array.from(value instanceof Uint8Array ? value.slice(0, 16) : []);
      } finally {
        try { await reader.cancel(); } catch { /* The response may already be closed. */ }
      }
    }
    return Array.from(new Uint8Array(await response.arrayBuffer()).slice(0, 16));
  }

  async function preflightCandidate(candidateUrl, timeoutMs = 15000) {
    const url = new URL(candidateUrl);
    if (!["http:", "https:"].includes(url.protocol)) throw new Error("PDF 候选地址协议无效");
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), Math.min(Math.max(Number(timeoutMs) || 15000, 1000), 30000));
    try {
      const response = await fetch(url.toString(), {
        method: "GET",
        headers: { Accept: "application/pdf", Range: "bytes=0-15" },
        credentials: "include",
        cache: "no-store",
        redirect: "follow",
        signal: controller.signal,
      });
      return {
        status: response.status,
        contentType: response.headers.get("content-type") || "",
        bytes: await responsePrefix(response),
        finalUrl: response.url || url.toString(),
      };
    } finally {
      clearTimeout(timer);
    }
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === PROBE_MESSAGE) {
      try {
        sendResponse({ ok: true, snapshot: collectSnapshot() });
      } catch (error) {
        sendResponse({ ok: false, error: error instanceof Error ? error.message : "出版社页面读取失败" });
      }
      return true;
    }
    if (message?.type === NAVIGATE_PDF_MESSAGE) {
      const entry = matchingPdfEntry(message.url, message.actionId);
      if (!entry) {
        sendResponse({ ok: false, error: "当前文章页没有找到可信的 PDF 入口" });
        return true;
      }
      sendResponse({ ok: true, url: entry.url, action: entry.action });
      setTimeout(() => {
        try {
          if (entry.element) {
            entry.element.scrollIntoView({ block: "center", inline: "nearest" });
            entry.element.click();
          } else {
            location.assign(entry.url);
          }
        } catch {
          location.assign(entry.url);
        }
      }, 0);
      return true;
    }
    if (message?.type !== PREFLIGHT_MESSAGE) return undefined;
    preflightCandidate(message.url, message.timeoutMs)
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error) => sendResponse({ ok: false, error: error instanceof Error ? error.message : "PDF 响应预检失败" }));
    return true;
  });
})();
