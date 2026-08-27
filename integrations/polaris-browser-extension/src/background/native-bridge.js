import { MINIMUM_NATIVE_BRIDGE_VERSION, NATIVE_HOST_NAME } from "../shared/constants.js";

const DEFAULT_STATUS_TTL_MS = 30_000;
const statusCache = new WeakMap();

export function isSupportedBridgeVersion(version, minimum = MINIMUM_NATIVE_BRIDGE_VERSION) {
  const parse = (value) => String(value || "").split(".").slice(0, 3).map((part) => Number.parseInt(part, 10));
  const current = parse(version);
  const required = parse(minimum);
  if (current.length < 3 || current.some(Number.isNaN)) return false;
  for (let index = 0; index < 3; index += 1) {
    if (current[index] !== required[index]) return current[index] > required[index];
  }
  return true;
}

export function sendNativeCommand(chromeApi, command, payload = {}, timeoutMs = 15000) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const port = chromeApi.runtime.connectNative(NATIVE_HOST_NAME);
    const timer = setTimeout(() => finish(new Error("本地下载桥响应超时")), timeoutMs);
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { port.disconnect(); } catch { /* already disconnected */ }
      if (error) reject(error);
      else resolve(value);
    };
    port.onMessage.addListener((message) => {
      if (message?.ok) finish(null, message);
      else finish(new Error(message?.error || "本地下载桥操作失败"));
    });
    port.onDisconnect.addListener(() => {
      const message = chromeApi.runtime.lastError?.message;
      if (!settled) finish(new Error(message || "本地下载桥未安装或已断开"));
    });
    port.postMessage({ command, ...payload });
  });
}

function nativeSession(chromeApi, timeoutMs = 30000) {
  const port = chromeApi.runtime.connectNative(NATIVE_HOST_NAME);
  let pending = null;
  let closed = false;
  const rejectPending = (error) => {
    if (!pending) return;
    clearTimeout(pending.timer);
    const reject = pending.reject;
    pending = null;
    reject(error);
  };
  port.onMessage.addListener((message) => {
    if (!pending) return;
    clearTimeout(pending.timer);
    const { resolve, reject } = pending;
    pending = null;
    if (message?.ok) resolve(message);
    else reject(new Error(message?.error || "本地下载桥操作失败"));
  });
  port.onDisconnect.addListener(() => {
    closed = true;
    rejectPending(new Error(chromeApi.runtime.lastError?.message || "本地下载桥已断开"));
  });
  return {
    request(command, payload = {}) {
      if (closed) return Promise.reject(new Error("本地下载桥会话已关闭"));
      if (pending) return Promise.reject(new Error("本地下载桥会话仍有未完成请求"));
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          pending = null;
          reject(new Error("本地下载桥响应超时"));
        }, timeoutMs);
        pending = { resolve, reject, timer };
        port.postMessage({ command, ...payload });
      });
    },
    close() {
      closed = true;
      rejectPending(new Error("本地下载桥会话已关闭"));
      try { port.disconnect(); } catch { /* already disconnected */ }
    },
  };
}

function bytesToBase64(bytes) {
  let binary = "";
  const block = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += block) {
    binary += String.fromCharCode(...bytes.subarray(offset, Math.min(bytes.length, offset + block)));
  }
  return btoa(binary);
}

export async function archiveCachedPdfWithBridge(chromeApi, response, {
  destinationId,
  taskCode,
  fileName,
  itemId,
  expectedDoi,
  expectedTitle,
  manualApproval = false,
  maxBytes,
  metadata,
  onProgress = null,
}) {
  if (!response?.body?.getReader) throw new Error("浏览器 PDF 缓存不可读取");
  const session = nativeSession(chromeApi, 120000);
  let uploadId = null;
  let bytes = 0;
  let sequence = 0;
  try {
    const begin = await session.request("begin_cached_upload", { taskCode, fileName, maxBytes });
    uploadId = begin.uploadId;
    const reader = response.body.getReader();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = value instanceof Uint8Array ? value : new Uint8Array(value || []);
      for (let offset = 0; offset < chunk.length; offset += 256 * 1024) {
        const part = chunk.subarray(offset, Math.min(chunk.length, offset + 256 * 1024));
        const appended = await session.request("append_cached_upload", {
          uploadId,
          sequence,
          data: bytesToBase64(part),
        });
        sequence = Number(appended.nextSequence);
        bytes = Number(appended.bytes);
        await onProgress?.({ bytes });
      }
    }
    const committed = await session.request("commit_cached_upload", {
      uploadId,
      expectedDoi,
      expectedTitle,
    });
    if (committed.status === "invalid") return committed;
    if (["mismatch", "inconclusive"].includes(committed.status) && !manualApproval) {
      await session.request("abort_cached_upload", { uploadId });
      uploadId = null;
      return committed;
    }
    return await session.request("finalize_cached_upload", {
      uploadId,
      destinationId,
      taskCode,
      fileName,
      itemId,
      expectedDoi,
      expectedTitle,
      manualApproval,
      maxBytes,
      metadata,
    });
  } catch (error) {
    if (uploadId) {
      try { await session.request("abort_cached_upload", { uploadId }); } catch { /* Session may already be gone. */ }
    }
    throw error;
  } finally {
    session.close();
  }
}

export function invalidateNativeBridgeStatus(chromeApi) {
  statusCache.delete(chromeApi);
}

export async function nativeBridgeStatus(chromeApi, { force = false, ttlMs = DEFAULT_STATUS_TTL_MS } = {}) {
  const now = Date.now();
  const cached = statusCache.get(chromeApi);
  if (!force && cached?.value && cached.expiresAt > now) return cached.value;
  if (!force && cached?.pending) return cached.pending;

  const pending = (async () => {
    try {
      const result = await sendNativeCommand(chromeApi, "get_status", {}, 3000);
      const version = result.version || "unknown";
      return {
        connected: true,
        compatible: isSupportedBridgeVersion(version),
        version,
        destinationCount: Number(result.destinationCount || 0),
        defaultDestination: result.defaultDestination || null,
      };
    } catch (error) {
      return { connected: false, error: error instanceof Error ? error.message : "本地下载桥不可用" };
    }
  })();
  statusCache.set(chromeApi, { pending, expiresAt: now + ttlMs });
  const value = await pending;
  statusCache.set(chromeApi, { value, expiresAt: Date.now() + ttlMs });
  return value;
}
