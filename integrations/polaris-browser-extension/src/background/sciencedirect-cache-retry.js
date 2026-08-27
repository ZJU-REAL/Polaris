const DEFAULT_RETRY_DELAYS_MS = [2500, 5000, 7500, 10000];
const MINIMUM_SIGNATURE_REMAINING_MS = 15000;

function abortError() {
  return new DOMException("缓存已停止", "AbortError");
}

function abortableDelay(milliseconds, signal) {
  if (signal?.aborted) return Promise.reject(abortError());
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      clearTimeout(timer);
      reject(abortError());
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, milliseconds);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export function isRetryableScienceDirectCacheError(error) {
  if (!error || error.name === "AbortError") return false;
  const detail = error instanceof Error ? error.message : String(error);
  return /HTTP\s+(?:401|403|408|409|425|429|5\d\d)|HTML|非 PDF|challenge|验证页面|fetch|network|Failed to fetch/i.test(detail);
}

export async function cacheScienceDirectWithRetry(operation, {
  signal = null,
  expiresAt = null,
  retryDelaysMs = DEFAULT_RETRY_DELAYS_MS,
  now = () => Date.now(),
  sleep = abortableDelay,
  onRetry = null,
} = {}) {
  if (typeof operation !== "function") throw new Error("ScienceDirect PDF 缓存操作无效");
  let attempt = 1;
  while (true) {
    try {
      return await operation({ attempt });
    } catch (error) {
      const delayMs = retryDelaysMs[attempt - 1];
      if (delayMs == null || !isRetryableScienceDirectCacheError(error)) throw error;
      const expiry = Date.parse(expiresAt || "");
      if (Number.isFinite(expiry) && expiry - now() <= delayMs + MINIMUM_SIGNATURE_REMAINING_MS) throw error;
      await onRetry?.({ attempt, nextAttempt: attempt + 1, delayMs, error });
      await sleep(delayMs, signal);
      attempt += 1;
    }
  }
}

export const SCIENCEDIRECT_CACHE_RETRY_DELAYS_MS = DEFAULT_RETRY_DELAYS_MS;
