const CACHE_NAME = "yfr-pdf-cache-v1";
const CACHE_ORIGIN = "https://cache.yfr.invalid";
const PDF_SIGNATURE = new TextEncoder().encode("%PDF-");
const CACHE_RESERVE_MIN_BYTES = 128 * 1024 * 1024;
const UNKNOWN_RESPONSE_RESERVE_BYTES = 32 * 1024 * 1024;
const HTTP_ERROR_BODY_LIMIT = 4096;

function cacheSegment(value) {
  return encodeURIComponent(String(value || "").slice(0, 500));
}

export function browserPdfCacheKey(taskId, itemId) {
  if (!taskId || !itemId) throw new Error("PDF 缓存标识不完整");
  return `${CACHE_ORIGIN}/${cacheSegment(taskId)}/${cacheSegment(itemId)}.pdf`;
}

export function hasPdfSignature(bytes) {
  if (!(bytes instanceof Uint8Array) || bytes.length < PDF_SIGNATURE.length) return false;
  return PDF_SIGNATURE.every((value, index) => bytes[index] === value);
}

function contentLength(response) {
  const value = Number(response.headers?.get?.("content-length") || 0);
  return Number.isFinite(value) && value > 0 ? value : 0;
}

async function responseErrorPrefix(response, limit = HTTP_ERROR_BODY_LIMIT) {
  try {
    if (!response?.body?.getReader) return String(await response.text()).slice(0, limit);
    const reader = response.body.getReader();
    const chunks = [];
    let bytes = 0;
    while (bytes < limit) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = value instanceof Uint8Array ? value : new Uint8Array(value || []);
      const remaining = limit - bytes;
      chunks.push(chunk.slice(0, remaining));
      bytes += Math.min(chunk.byteLength, remaining);
      if (chunk.byteLength > remaining) break;
    }
    try { await reader.cancel(); } catch { /* Error response may already be closed. */ }
    const merged = new Uint8Array(bytes);
    let offset = 0;
    for (const chunk of chunks) {
      merged.set(chunk, offset);
      offset += chunk.byteLength;
    }
    return new TextDecoder().decode(merged);
  } catch {
    return "";
  }
}

async function verifyResponsePdfSignature(response) {
  const probe = response.clone();
  if (!probe.body?.getReader) {
    const bytes = new Uint8Array(await probe.arrayBuffer()).slice(0, PDF_SIGNATURE.length);
    if (!hasPdfSignature(bytes)) throw new Error("PDF 入口实际返回 HTML 或非 PDF 内容");
    return;
  }
  const reader = probe.body.getReader();
  const prefix = new Uint8Array(PDF_SIGNATURE.length);
  let offset = 0;
  try {
    while (offset < PDF_SIGNATURE.length) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = value instanceof Uint8Array ? value : new Uint8Array(value || []);
      const take = Math.min(PDF_SIGNATURE.length - offset, chunk.byteLength);
      prefix.set(chunk.slice(0, take), offset);
      offset += take;
    }
  } finally {
    try { void reader.cancel().catch(() => undefined); } catch { /* The signature probe may already be complete. */ }
  }
  if (offset < PDF_SIGNATURE.length || !hasPdfSignature(prefix)) {
    throw new Error("PDF 入口实际返回 HTML 或非 PDF 内容");
  }
}

export async function classifyPdfHttpError(response) {
  const body = await responseErrorPrefix(response);
  if (/ExpiredToken|RequestExpired|request\s+has\s+expired|token\s+has\s+expired/i.test(body)) {
    return "ScienceDirect 临时签名已过期";
  }
  if (/SignatureDoesNotMatch|signature\s+(?:does\s+not\s+match|mismatch)/i.test(body)) {
    return "ScienceDirect 临时签名与当前请求不匹配";
  }
  if (/AccessDenied|access\s+denied|forbidden/i.test(body)) {
    return "ScienceDirect 拒绝了扩展请求上下文";
  }
  return "";
}

export async function browserStorageEstimate(storageManager = globalThis.navigator?.storage) {
  if (!storageManager?.estimate) return null;
  try {
    const estimate = await storageManager.estimate();
    const usage = Math.max(0, Number(estimate?.usage || 0));
    const quota = Math.max(0, Number(estimate?.quota || 0));
    const available = quota > 0 ? Math.max(0, quota - usage) : null;
    let persisted = null;
    try { persisted = storageManager.persisted ? await storageManager.persisted() : null; } catch { /* Optional browser signal. */ }
    return {
      usage,
      quota,
      available,
      percent: quota > 0 ? Math.min(100, Math.round((usage / quota) * 1000) / 10) : null,
      persisted,
    };
  } catch {
    return null;
  }
}

export async function ensureBrowserCacheCapacity({
  expectedBytes,
  maxBytes,
  storageManager = globalThis.navigator?.storage,
}) {
  const estimate = await browserStorageEstimate(storageManager);
  if (!estimate || estimate.available == null || estimate.quota <= 0) return estimate;
  const reserve = Math.min(
    Math.max(CACHE_RESERVE_MIN_BYTES, Math.floor(estimate.quota * 0.02)),
    512 * 1024 * 1024,
  );
  const required = Math.max(1, Number(expectedBytes || Math.min(maxBytes, UNKNOWN_RESPONSE_RESERVE_BYTES)));
  if (estimate.available < required + reserve) {
    throw new Error("浏览器 PDF 缓存空间不足，请先归档或清理已缓存文献");
  }
  return estimate;
}

function cachedHeaders(response, bytes) {
  const headers = new Headers();
  headers.set("content-type", "application/pdf");
  if (bytes > 0) headers.set("content-length", String(bytes));
  const sourceMime = response.headers?.get?.("content-type");
  if (sourceMime) headers.set("x-yfr-source-mime", sourceMime.slice(0, 200));
  return headers;
}

async function cacheBufferedResponse(cache, key, response, maxBytes, onProgress) {
  const buffer = await response.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  if (!hasPdfSignature(bytes)) throw new Error("PDF 入口实际返回的内容缺少 %PDF- 文件签名");
  if (bytes.byteLength <= 1024) throw new Error("PDF 响应过小，无法作为有效论文缓存");
  if (bytes.byteLength > maxBytes) throw new Error("PDF 超过任务配置的大小上限");
  await cache.put(key, new Response(buffer, { status: 200, headers: cachedHeaders(response, bytes.byteLength) }));
  await onProgress?.({ bytes: bytes.byteLength, totalBytes: bytes.byteLength, percent: 100 });
  return bytes.byteLength;
}

async function cacheStreamingResponse(cache, key, response, maxBytes, onProgress) {
  const reader = response.body.getReader();
  const totalBytes = contentLength(response);
  if (totalBytes > maxBytes) {
    await reader.cancel();
    throw new Error("PDF 超过任务配置的大小上限");
  }
  let bytes = 0;
  let prefix = new Uint8Array();
  let signatureChecked = false;
  let lastProgressAt = 0;
  const stream = new ReadableStream({
    async pull(controller) {
      const { done, value } = await reader.read();
      if (done) {
        if (!signatureChecked || bytes <= 1024) {
          controller.error(new Error("PDF 响应不完整或文件过小"));
          return;
        }
        await onProgress?.({ bytes, totalBytes: totalBytes || bytes, percent: 100 });
        controller.close();
        return;
      }
      const chunk = value instanceof Uint8Array ? value : new Uint8Array(value || []);
      bytes += chunk.byteLength;
      if (bytes > maxBytes) {
        await reader.cancel();
        controller.error(new Error("PDF 超过任务配置的大小上限"));
        return;
      }
      if (!signatureChecked) {
        const needed = Math.max(0, PDF_SIGNATURE.length - prefix.length);
        const nextPrefix = new Uint8Array(prefix.length + Math.min(needed, chunk.length));
        nextPrefix.set(prefix);
        nextPrefix.set(chunk.slice(0, needed), prefix.length);
        prefix = nextPrefix;
        if (prefix.length >= PDF_SIGNATURE.length) {
          if (!hasPdfSignature(prefix)) {
            await reader.cancel();
            controller.error(new Error("PDF 入口实际返回 HTML 或非 PDF 内容"));
            return;
          }
          signatureChecked = true;
        }
      }
      controller.enqueue(chunk);
      const now = Date.now();
      if (now - lastProgressAt >= 400 || (totalBytes && bytes >= totalBytes)) {
        lastProgressAt = now;
        await onProgress?.({
          bytes,
          totalBytes,
          percent: totalBytes ? Math.min(99, Math.floor((bytes / totalBytes) * 100)) : null,
        });
      }
    },
    async cancel(reason) {
      try { await reader.cancel(reason); } catch { /* Response may already be closed. */ }
    },
  });
  await cache.put(key, new Response(stream, {
    status: 200,
    headers: cachedHeaders(response, totalBytes || 0),
  }));
  return bytes;
}

export async function cachePdfInBrowser({
  taskId,
  itemId,
  url,
  maxBytes,
  signal,
  fetchImpl = fetch,
  cacheStorage = caches,
  storageManager = globalThis.navigator?.storage,
  onProgress = null,
}) {
  const key = browserPdfCacheKey(taskId, itemId);
  const cache = await cacheStorage.open(CACHE_NAME);
  await cache.delete(key);
  const response = await fetchImpl(url, {
    method: "GET",
    headers: { Accept: "application/pdf" },
    credentials: "include",
    cache: "force-cache",
    redirect: "follow",
    signal,
  });
  if (!response.ok) {
    const reason = await classifyPdfHttpError(response);
    throw new Error(`PDF 缓存请求失败（HTTP ${response.status}${reason ? `：${reason}` : ""}）`);
  }
  const declaredBytes = contentLength(response);
  if (declaredBytes > maxBytes) throw new Error("PDF 超过任务配置的大小上限");
  await ensureBrowserCacheCapacity({ expectedBytes: declaredBytes, maxBytes, storageManager });
  try {
    await verifyResponsePdfSignature(response);
    const bytes = response.body?.getReader
      ? await cacheStreamingResponse(cache, key, response, maxBytes, onProgress)
      : await cacheBufferedResponse(cache, key, response, maxBytes, onProgress);
    return {
      cacheName: CACHE_NAME,
      cacheKey: key,
      bytes,
      mime: "application/pdf",
      sourceMime: response.headers?.get?.("content-type") || "",
      finalUrl: response.url || url,
      signatureMatched: true,
    };
  } catch (error) {
    await cache.delete(key);
    throw error;
  }
}

export async function cachePdfBytesInBrowser({
  taskId,
  itemId,
  bytes,
  maxBytes,
  sourceMime = "application/pdf",
  cacheStorage = caches,
  storageManager = globalThis.navigator?.storage,
  onProgress = null,
}) {
  const value = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes || []);
  if (!hasPdfSignature(value)) throw new Error("浏览器导航响应缺少 %PDF- 文件签名");
  if (value.byteLength <= 1024) throw new Error("PDF 响应过小，无法作为有效论文缓存");
  if (value.byteLength > maxBytes) throw new Error("PDF 超过任务配置的大小上限");
  await ensureBrowserCacheCapacity({ expectedBytes: value.byteLength, maxBytes, storageManager });
  const key = browserPdfCacheKey(taskId, itemId);
  const cache = await cacheStorage.open(CACHE_NAME);
  await cache.delete(key);
  const buffer = value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength);
  const response = new Response(buffer, {
    status: 200,
    headers: {
      "content-type": "application/pdf",
      "content-length": String(value.byteLength),
      "x-yfr-source-mime": String(sourceMime || "application/pdf").slice(0, 200),
    },
  });
  await cache.put(key, response);
  await onProgress?.({ bytes: value.byteLength, totalBytes: value.byteLength, percent: 100 });
  return {
    cacheName: CACHE_NAME,
    cacheKey: key,
    bytes: value.byteLength,
    mime: "application/pdf",
    sourceMime,
    finalUrl: null,
    signatureMatched: true,
  };
}

export async function getBrowserCachedPdf(cacheKey, cacheStorage = caches) {
  if (!cacheKey) return null;
  const cache = await cacheStorage.open(CACHE_NAME);
  return cache.match(cacheKey);
}

export async function copyBrowserCachedPdf({ sourceKey, taskId, itemId, cacheStorage = caches }) {
  if (!sourceKey) return null;
  const cache = await cacheStorage.open(CACHE_NAME);
  const source = await cache.match(sourceKey);
  if (!source) return null;
  const targetKey = browserPdfCacheKey(taskId, itemId);
  await cache.delete(targetKey);
  await cache.put(targetKey, source.clone());
  return {
    cacheName: CACHE_NAME,
    cacheKey: targetKey,
    bytes: Number(source.headers.get("content-length") || 0),
    mime: source.headers.get("content-type") || "application/pdf",
    sourceMime: source.headers.get("x-yfr-source-mime") || source.headers.get("content-type") || "application/pdf",
    signatureMatched: true,
  };
}

export async function deleteBrowserCachedPdf(cacheKey, cacheStorage = caches) {
  if (!cacheKey) return false;
  const cache = await cacheStorage.open(CACHE_NAME);
  return cache.delete(cacheKey);
}

export const BROWSER_PDF_CACHE_NAME = CACHE_NAME;
