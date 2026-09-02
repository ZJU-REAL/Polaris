import { normalizeHttpUrl, normalizePolarisOrigin } from "./origin.js";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function object(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function text(value, maxLength) {
  return String(value || "").trim().slice(0, maxLength);
}

function uuid(value) {
  const normalized = text(value, 64);
  return UUID_RE.test(normalized) ? normalized.toLowerCase() : null;
}

function identifier(value, maxLength) {
  const normalized = text(value, maxLength);
  return normalized || null;
}

function validateWindow(payload, now) {
  const issuedAt = Date.parse(payload.issued_at || "");
  const expiresAt = Date.parse(payload.expires_at || "");
  if (!Number.isFinite(issuedAt) || !Number.isFinite(expiresAt)) return false;
  return issuedAt <= now + 60_000 && expiresAt >= now && expiresAt - issuedAt <= 10 * 60_000;
}

function normalizeCandidates(value) {
  if (!Array.isArray(value)) return [];
  return value.slice(0, 40).map((entry) => {
    const raw = object(entry) ? entry : { url: entry };
    const url = normalizeHttpUrl(raw.url);
    if (!url) return null;
    return {
      url,
      source: text(raw.source, 80) || "polaris",
      kind: text(raw.kind, 40) || "unknown",
    };
  }).filter(Boolean);
}

export function validateDownloadBatch(payload, pageOrigin, now = Date.now()) {
  if (!object(payload) || payload.version !== 2) {
    return { ok: false, error: "不支持的 Polaris 批次版本" };
  }
  let instanceOrigin;
  let eventOrigin;
  try {
    instanceOrigin = normalizePolarisOrigin(payload.instance_origin);
    eventOrigin = normalizePolarisOrigin(pageOrigin);
  } catch (error) {
    return { ok: false, error: error.message };
  }
  if (instanceOrigin !== eventOrigin) return { ok: false, error: "批次实例与当前页面不一致" };
  if (!validateWindow(payload, now)) return { ok: false, error: "Polaris 下载批次已过期" };

  const batchNonce = text(payload.batch_nonce, 200);
  const backendBatchId = payload.backend_batch_id == null ? null : uuid(payload.backend_batch_id);
  const rawPapers = Array.isArray(payload.papers) ? payload.papers : [];
  if (batchNonce.length < 16 || (payload.backend_batch_id && !backendBatchId)) {
    return { ok: false, error: "Polaris 下载批次缺少必要标识" };
  }
  if (rawPapers.length < 1 || rawPapers.length > 500) {
    return { ok: false, error: "Polaris 下载批次论文数量必须为 1-500" };
  }

  const papers = [];
  const bindings = new Set();
  for (const raw of rawPapers) {
    if (!object(raw)) return { ok: false, error: "Polaris 论文数据格式无效" };
    const libraryId = uuid(raw.library_id);
    const paperId = uuid(raw.paper_id);
    const nonce = text(raw.nonce, 200);
    const identity = object(raw.identity) ? raw.identity : {};
    const title = text(identity.title, 2000);
    if (!libraryId || !paperId || nonce.length < 16 || !title) {
      return { ok: false, error: "Polaris 论文缺少独立归档标识" };
    }
    const binding = `${libraryId}:${paperId}`;
    if (bindings.has(binding)) return { ok: false, error: "Polaris 下载批次包含重复论文目标" };
    bindings.add(binding);
    papers.push({
      libraryId,
      paperId,
      nonce,
      title,
      doi: identifier(identity.doi, 512),
      pmid: identifier(identity.pmid, 64),
      pmcid: identifier(identity.pmcid, 64),
      arxivId: identifier(identity.arxiv_id, 128),
      articleUrl: normalizeHttpUrl(raw.article_url),
      candidates: normalizeCandidates(raw.pdf_candidates),
    });
  }
  return {
    ok: true,
    value: { instanceOrigin, batchNonce, backendBatchId, papers },
  };
}

export function createLocalTask(batch, randomUuid = () => crypto.randomUUID(), now = new Date()) {
  const taskId = randomUuid();
  return {
    id: taskId,
    source: "polaris",
    instanceOrigin: batch.instanceOrigin,
    batchNonce: batch.batchNonce,
    backendBatchId: batch.backendBatchId,
    createdAt: now.toISOString(),
    updatedAt: now.toISOString(),
    items: batch.papers.map((paper) => ({
      id: randomUuid(),
      taskId,
      status: "queued",
      error: null,
      cache: null,
      archive: null,
      ...paper,
    })),
  };
}
