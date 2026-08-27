const PDF_PREFIX = "%PDF-";
const NON_PDF_CONTENT_TYPES = ["text/html", "application/xhtml+xml", "application/json", "text/plain"];

function prefixText(bytes) {
  return new TextDecoder("ascii").decode(bytes.slice(0, PDF_PREFIX.length));
}

export function classifyPdfPreflight({ status = 0, contentType = "", bytes = new Uint8Array(), finalUrl = "" }) {
  const mime = String(contentType || "").split(";", 1)[0].trim().toLowerCase();
  const signatureMatched = bytes.length >= PDF_PREFIX.length && prefixText(bytes) === PDF_PREFIX;
  if (signatureMatched) {
    return { kind: "verified", reason: "PDF 响应签名已校验", mime, finalUrl, signatureMatched: true };
  }
  if ([401, 403].includes(Number(status))) {
    return { kind: "access-required", reason: "PDF 入口需要机构登录或人工验证", mime, finalUrl, signatureMatched: false };
  }
  if (NON_PDF_CONTENT_TYPES.some((value) => mime.startsWith(value))) {
    return { kind: "not-pdf", reason: "PDF 入口实际返回 HTML 或文本验证页", mime, finalUrl, signatureMatched: false };
  }
  if (bytes.length >= PDF_PREFIX.length) {
    return { kind: "not-pdf", reason: "PDF 入口响应缺少 %PDF- 文件签名", mime, finalUrl, signatureMatched: false };
  }
  if (Number(status) >= 400) {
    return { kind: "unavailable", reason: `PDF 入口暂不可用（HTTP ${status}）`, mime, finalUrl, signatureMatched: false };
  }
  return { kind: "unknown", reason: "已找到 PDF 入口，但响应签名尚未确认", mime, finalUrl, signatureMatched: false };
}

export function shouldRetireCandidateAfterPreflight(candidate, publisherKey, result) {
  if (result?.kind !== "not-pdf") return false;
  if (candidate?.retriableAfterAccess) return false;
  let scienceDirectViewPdf = publisherKey === "sciencedirect" && candidate?.sourceDetail === "sciencedirect-view-pdf";
  try {
    const url = new URL(candidate?.url);
    scienceDirectViewPdf ||= url.hostname.endsWith("sciencedirect.com") && /\/science\/article\/pii\/[a-z0-9]+\/pdfft\/?$/i.test(url.pathname);
  } catch {
    // Invalid URLs are rejected by the normal URL-security layer.
  }
  return !scienceDirectViewPdf;
}

async function readResponsePrefix(response) {
  if (response.body?.getReader) {
    const reader = response.body.getReader();
    try {
      const { value } = await reader.read();
      return value instanceof Uint8Array ? value.slice(0, 16) : new Uint8Array();
    } finally {
      try { await reader.cancel(); } catch { /* The response may already be closed. */ }
    }
  }
  if (typeof response.arrayBuffer === "function") {
    return new Uint8Array(await response.arrayBuffer()).slice(0, 16);
  }
  return new Uint8Array();
}

export async function preflightPdfCandidate(url, { fetchImpl = fetch, timeoutMs = 15000 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetchImpl(url, {
      method: "GET",
      headers: { Accept: "application/pdf", Range: "bytes=0-15" },
      credentials: "include",
      cache: "no-store",
      redirect: "follow",
      signal: controller.signal,
    });
    const bytes = await readResponsePrefix(response);
    return classifyPdfPreflight({
      status: response.status,
      contentType: response.headers?.get?.("content-type") || "",
      bytes,
      finalUrl: response.url || url,
    });
  } catch (error) {
    return {
      kind: "unknown",
      reason: error?.name === "AbortError" ? "PDF 响应预检超时" : "当前浏览器会话无法预检 PDF 响应",
      mime: "",
      finalUrl: url,
      signatureMatched: false,
    };
  } finally {
    clearTimeout(timer);
  }
}
