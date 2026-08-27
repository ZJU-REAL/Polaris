import { stableHash } from "../shared/normalization.js";

const PROTOCOL_VERSION = 1;
const STORAGE_KEY = "yfrZoteroConnection";
const EXTENSION_ID = "ikinkjjfnpikbjlekpbdojnflldbnkjg";
const PATHS = Object.freeze({
  status: "/yfr-zotero/v1/status",
  pair: "/yfr-zotero/v1/pair",
  sync: "/yfr-zotero/v1/sync-linked-pdf",
});
const LOOPBACK_ENDPOINTS = Object.freeze([23119, 23120, 23121, 23122, 23123]
  .map((port) => `http://127.0.0.1:${port}`));
const LOCAL_SYNC_TIMEOUT_MS = 120_000;
const FORCE_PROBE_ATTEMPTS = 3;
const encoder = new TextEncoder();
let cachedStatus = null;
let pendingProbe = null;

export function supportsStoredPdfSync(version) {
  const match = String(version || "").match(/^(\d+)\.(\d+)\.(\d+)/);
  if (!match) return false;
  const current = match.slice(1).map(Number);
  const minimum = [0, 3, 0];
  for (let index = 0; index < minimum.length; index += 1) {
    if (current[index] !== minimum[index]) return current[index] > minimum[index];
  }
  return true;
}

export class ZoteroSyncError extends Error {
  constructor(message, { retryable = false, authentication = false } = {}) {
    super(message);
    this.retryable = retryable;
    this.authentication = authentication;
  }
}

function bytesToHex(buffer) {
  return Array.from(new Uint8Array(buffer), (value) => value.toString(16).padStart(2, "0")).join("");
}

function bytesToBase64Url(bytes) {
  let binary = "";
  for (const value of bytes) binary += String.fromCharCode(value);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

async function sha256Hex(value) {
  return bytesToHex(await crypto.subtle.digest("SHA-256", encoder.encode(String(value))));
}

async function hmacSha256Hex(secret, value) {
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(String(secret)),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return bytesToHex(await crypto.subtle.sign("HMAC", key, encoder.encode(String(value))));
}

function randomValue(bytes = 18) {
  const value = new Uint8Array(bytes);
  crypto.getRandomValues(value);
  return bytesToBase64Url(value);
}

function commonHeaders() {
  return {
    "Zotero-Allowed-Request": "1",
    "X-YFR-Extension-Id": EXTENSION_ID,
    "Cache-Control": "no-store",
  };
}

async function fetchWithTimeout(url, options = {}, timeoutMs = 1500) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal, cache: "no-store" });
  } finally {
    clearTimeout(timer);
  }
}

async function responseJson(response) {
  let body = null;
  try { body = await response.json(); } catch { /* Invalid local service response. */ }
  return body;
}

export async function storedZoteroConnection(chromeApi) {
  const record = (await chromeApi.storage.local.get(STORAGE_KEY))?.[STORAGE_KEY];
  if (!record || typeof record !== "object") return { autoSync: true };
  return {
    endpoint: LOOPBACK_ENDPOINTS.includes(record.endpoint) ? record.endpoint : null,
    clientId: /^[A-Za-z0-9_-]{16,96}$/.test(String(record.clientId || "")) ? record.clientId : null,
    token: /^[A-Za-z0-9_-]{32,128}$/.test(String(record.token || "")) ? record.token : null,
    autoSync: record.autoSync !== false,
    pairedAt: record.pairedAt || null,
  };
}

async function saveConnection(chromeApi, patch) {
  const current = await storedZoteroConnection(chromeApi);
  const next = { ...current, ...patch };
  await chromeApi.storage.local.set({ [STORAGE_KEY]: next });
  cachedStatus = null;
  return next;
}

export async function disconnectZotero(chromeApi) {
  const current = await storedZoteroConnection(chromeApi);
  await chromeApi.storage.local.set({ [STORAGE_KEY]: { autoSync: current.autoSync !== false } });
  cachedStatus = null;
  return { ok: true };
}

async function probeEndpoint(endpoint) {
  try {
    const response = await fetchWithTimeout(`${endpoint}${PATHS.status}`, {
      method: "GET",
      headers: commonHeaders(),
    });
    if (!response.ok) return null;
    const body = await responseJson(response);
    if (body?.ok !== true
      || body.service !== "yfr-zotero-companion"
      || body.protocolVersion !== PROTOCOL_VERSION) return null;
    return { endpoint, pluginVersion: body.pluginVersion || "unknown", pairingAvailable: Boolean(body.pairingAvailable) };
  } catch {
    return null;
  }
}

function wait(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function probeEndpoints(endpoints) {
  const results = await Promise.all(endpoints.map(probeEndpoint));
  return results.find(Boolean) || null;
}

export async function zoteroStatus(chromeApi, { force = false, ttlMs = 12_000 } = {}) {
  const now = Date.now();
  if (!force && cachedStatus?.expiresAt > now) return cachedStatus.value;
  if (!force && pendingProbe) return pendingProbe;
  pendingProbe = (async () => {
    const stored = await storedZoteroConnection(chromeApi);
    const endpoints = Array.from(new Set([stored.endpoint, ...LOOPBACK_ENDPOINTS].filter(Boolean)));
    let found = null;
    const attempts = force ? FORCE_PROBE_ATTEMPTS : 1;
    for (let attempt = 0; attempt < attempts && !found; attempt += 1) {
      found = await probeEndpoints(endpoints);
      if (!found && attempt + 1 < attempts) await wait(250 * (attempt + 1));
    }
    const value = {
      available: Boolean(found),
      endpoint: found?.endpoint || null,
      pluginVersion: found?.pluginVersion || null,
      pairingAvailable: Boolean(found?.pairingAvailable),
      paired: Boolean(found && stored.clientId && stored.token),
      autoSync: stored.autoSync !== false,
      error: found ? null : "未检测到 Zotero YFR Companion",
    };
    cachedStatus = { value, expiresAt: Date.now() + ttlMs };
    return value;
  })().finally(() => { pendingProbe = null; });
  return pendingProbe;
}

export async function pairWithZotero(chromeApi, pairingCode) {
  const code = String(pairingCode || "").trim();
  if (!/^\d{6}$/.test(code)) throw new Error("请输入 Zotero 中显示的 6 位配对码");
  const status = await zoteroStatus(chromeApi, { force: true });
  if (!status.available || !status.endpoint) throw new ZoteroSyncError("未检测到 Zotero YFR Companion", { retryable: true });
  const stored = await storedZoteroConnection(chromeApi);
  const clientId = stored.clientId || randomValue(18);
  const rawBody = JSON.stringify({
    pairingCode: code,
    clientId,
    clientName: `${navigator.userAgentData?.brands?.[0]?.brand || "Chromium"} · Polaris 扩展`,
    extensionId: EXTENSION_ID,
  });
  let response;
  try {
    response = await fetchWithTimeout(`${status.endpoint}${PATHS.pair}`, {
      method: "POST",
      headers: { ...commonHeaders(), "Content-Type": "application/octet-stream" },
      body: rawBody,
    }, 8000);
  } catch {
    throw new ZoteroSyncError("Zotero 配对请求失败，请确认 Zotero 仍在运行", { retryable: true });
  }
  const body = await responseJson(response);
  if (!response.ok || body?.ok !== true) throw new Error(body?.error || "Zotero 配对失败");
  if (!/^[A-Za-z0-9_-]{16,96}$/.test(String(body.clientId || ""))
    || !/^[A-Za-z0-9_-]{32,128}$/.test(String(body.token || ""))) {
    throw new Error("Zotero 返回了无效的配对凭据");
  }
  await saveConnection(chromeApi, {
    endpoint: status.endpoint,
    clientId: body.clientId,
    token: body.token,
    autoSync: stored.autoSync !== false,
    pairedAt: new Date().toISOString(),
  });
  return zoteroStatus(chromeApi, { force: true });
}

export async function setZoteroAutoSync(chromeApi, enabled) {
  await saveConnection(chromeApi, { autoSync: Boolean(enabled) });
  return zoteroStatus(chromeApi, { force: true });
}

export function buildZoteroSyncPayload(job, item) {
  const file = item?.file || {};
  if (!file.filename || !file.sha256 || !file.bytes) throw new Error("归档 PDF 缺少本地文件校验信息");
  const requestSeed = `${job.id}:${item.id}:${file.sha256}`;
  return {
    protocolVersion: PROTOCOL_VERSION,
    requestId: `yfr-${stableHash(requestSeed)}-${String(file.sha256).slice(0, 24)}`,
    taskId: job.id,
    yfrPaperId: item.yfrPaperId || item.paperKey || null,
    source: {
      area: job.origin?.area || null,
      searchId: job.origin?.searchId || null,
      runId: job.origin?.runId || null,
      topic: job.origin?.topic || null,
    },
    paper: {
      doi: item.doi || null,
      title: item.title,
      year: item.year || null,
      authors: Array.isArray(item.authors) ? item.authors : [],
      venue: item.venue || null,
      publisher: item.publisher || null,
      articleUrl: item.articleUrl || null,
    },
    file: {
      path: file.filename,
      bytes: Number(file.bytes),
      sha256: String(file.sha256).toLowerCase(),
      verificationLevel: file.verificationLevel,
    },
    identityVerification: {
      decisionBasis: item.identityVerification?.decisionBasis || null,
      detectedDoi: item.identityVerification?.detectedDoi || null,
      detectedDois: item.identityVerification?.detectedDois || [],
      doiMatched: Boolean(item.identityVerification?.doiMatched),
      titleSimilarity: Number(item.identityVerification?.titleSimilarity || 0),
      manuallyConfirmed: item.identityApproval?.method === "user" || file.verificationLevel === "manual-confirmed",
    },
  };
}

export async function syncLinkedPdfToZotero(chromeApi, job, item) {
  const status = await zoteroStatus(chromeApi);
  const stored = await storedZoteroConnection(chromeApi);
  if (!status.available || !status.endpoint) {
    throw new ZoteroSyncError("Zotero 当前未运行，已保留待同步状态", { retryable: true });
  }
  if (!stored.clientId || !stored.token) {
    throw new ZoteroSyncError("浏览器尚未与 Zotero 配对", { retryable: true, authentication: true });
  }
  const payload = buildZoteroSyncPayload(job, item);
  const rawBody = JSON.stringify(payload);
  const timestamp = String(Date.now());
  const nonce = randomValue(18);
  const bodyHash = await sha256Hex(rawBody);
  const input = ["POST", PATHS.sync, timestamp, nonce, bodyHash].join("\n");
  const signature = await hmacSha256Hex(stored.token, input);
  let response;
  try {
    response = await fetchWithTimeout(`${status.endpoint}${PATHS.sync}`, {
      method: "POST",
      headers: {
        ...commonHeaders(),
        "Content-Type": "application/octet-stream",
        "X-YFR-Client-Id": stored.clientId,
        "X-YFR-Timestamp": timestamp,
        "X-YFR-Nonce": nonce,
        "X-YFR-Body-SHA256": bodyHash,
        "X-YFR-Signature": signature,
      },
      body: rawBody,
    }, LOCAL_SYNC_TIMEOUT_MS);
  } catch {
    cachedStatus = null;
    throw new ZoteroSyncError("Zotero 本机接口暂时不可用，已保留待同步状态", { retryable: true });
  }
  const body = await responseJson(response);
  if (response.status === 401 || response.status === 403) {
    await disconnectZotero(chromeApi);
    throw new ZoteroSyncError(body?.error || "浏览器与 Zotero 的配对已失效", { retryable: true, authentication: true });
  }
  if (!response.ok || body?.ok !== true) {
    throw new ZoteroSyncError(body?.error || "Zotero 拒绝关联该 PDF", { retryable: false });
  }
  return body;
}
