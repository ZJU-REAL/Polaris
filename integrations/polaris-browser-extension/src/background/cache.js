import { inspectPdf, MAX_PDF_BYTES } from "../shared/pdf.js";

const CACHE_NAME = "polaris-pdf-cache-v1";
const CACHE_ORIGIN = "https://polaris-extension.invalid";

export function pdfCacheKey(taskId, itemId) {
  if (!taskId || !itemId) throw new Error("PDF 缓存标识不完整");
  return `${CACHE_ORIGIN}/pdf/${encodeURIComponent(taskId)}/${encodeURIComponent(itemId)}.pdf`;
}

export async function cachePdfBytes({ taskId, itemId, value, sourceUrl = null, cacheStorage = caches }) {
  const inspected = await inspectPdf(value);
  const cacheKey = pdfCacheKey(taskId, itemId);
  const cache = await cacheStorage.open(CACHE_NAME);
  const source = inspected.bytes.buffer.slice(
    inspected.bytes.byteOffset,
    inspected.bytes.byteOffset + inspected.bytes.byteLength,
  );
  await cache.put(cacheKey, new Response(source, {
    headers: {
      "content-type": "application/pdf",
      "content-length": String(inspected.byteSize),
      ...(sourceUrl ? { "x-polaris-source-url": sourceUrl.slice(0, 4000) } : {}),
    },
  }));
  return { cacheKey, byteSize: inspected.byteSize, sha256: inspected.sha256, sourceUrl };
}

export async function fetchAndCachePdf({
  taskId,
  itemId,
  url,
  fetchImpl = fetch,
  cacheStorage = caches,
  maxBytes = MAX_PDF_BYTES,
}) {
  const response = await fetchImpl(url, {
    method: "GET",
    headers: { Accept: "application/pdf" },
    credentials: "include",
    redirect: "follow",
    cache: "no-store",
  });
  if (!response.ok) throw new Error(`PDF 下载失败（HTTP ${response.status}）`);
  const declaredBytes = Number(response.headers.get("content-length") || 0);
  if (Number.isFinite(declaredBytes) && declaredBytes > maxBytes) {
    throw new Error("PDF 超过 150 MB 大小上限");
  }
  const buffer = await response.arrayBuffer();
  return cachePdfBytes({
    taskId,
    itemId,
    value: buffer,
    sourceUrl: response.url || url,
    cacheStorage,
  });
}

export async function getCachedPdf(cacheKey, cacheStorage = caches) {
  if (!cacheKey) return null;
  const cache = await cacheStorage.open(CACHE_NAME);
  return cache.match(cacheKey);
}

export async function deleteCachedPdf(cacheKey, cacheStorage = caches) {
  if (!cacheKey) return false;
  const cache = await cacheStorage.open(CACHE_NAME);
  return cache.delete(cacheKey);
}

export const PDF_CACHE_NAME = CACHE_NAME;
