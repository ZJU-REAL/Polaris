import { canonicalDoi, cleanText } from "./normalization.js";
import { parseSafeHttpUrl } from "./url-security.js";

export const POLARIS_TASK_EVENT = "polaris:download-task:v1";
export const POLARIS_ACK_EVENT = "polaris:download-task-ack:v1";
export const POLARIS_PROBE_EVENT = "polaris:download-extension-probe:v1";
export const POLARIS_READY_EVENT = "polaris:download-extension-ready:v1";

function object(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function uuid(value) {
  const text = cleanText(value, 64);
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(text)
    ? text.toLowerCase()
    : null;
}

export function normalizePolarisOrigin(value) {
  const url = parseSafeHttpUrl(value, { allowLocalDevelopment: true });
  if (!url) throw new Error("Polaris 工作台地址无效");
  const parsed = new URL(url);
  if (parsed.protocol === "http:" && !["localhost", "127.0.0.1"].includes(parsed.hostname)) {
    throw new Error("公网 Polaris 工作台必须使用 HTTPS");
  }
  return parsed.origin;
}

export function validatePolarisTask(payload, pageOrigin, now = Date.now()) {
  if (!object(payload) || payload.version !== 1) return { ok: false, error: "不支持的 Polaris 任务版本" };
  let origin;
  try { origin = normalizePolarisOrigin(payload.instance_origin); } catch (error) { return { ok: false, error: error.message }; }
  if (origin !== pageOrigin) return { ok: false, error: "任务实例与当前页面不一致" };
  const issued = Date.parse(payload.issued_at || "");
  const expires = Date.parse(payload.expires_at || "");
  if (!Number.isFinite(issued) || !Number.isFinite(expires) || issued > now + 60_000 || expires < now || expires - issued > 10 * 60_000) {
    return { ok: false, error: "Polaris 下载任务已过期" };
  }
  const libraryId = uuid(payload.library_id);
  const paperId = uuid(payload.paper_id);
  const searchHitId = payload.search_hit_id == null ? null : uuid(payload.search_hit_id);
  const nonce = cleanText(payload.nonce, 200);
  const identity = object(payload.identity) ? payload.identity : {};
  const title = cleanText(identity.title, 2000);
  if (!libraryId || !paperId || !nonce || nonce.length < 16 || !title || (payload.search_hit_id && !searchHitId)) {
    return { ok: false, error: "Polaris 下载任务缺少必要标识" };
  }
  const articleUrl = parseSafeHttpUrl(payload.article_url, { allowLocalDevelopment: true });
  const candidates = Array.isArray(payload.pdf_candidates)
    ? payload.pdf_candidates.slice(0, 40).map((entry) => {
        const raw = object(entry) ? entry : { url: entry };
        const url = parseSafeHttpUrl(raw.url, { allowLocalDevelopment: true });
        return url ? { url, source: cleanText(raw.source, 80) || "polaris", kind: cleanText(raw.kind, 40) || "unknown" } : null;
      }).filter(Boolean)
    : [];
  return {
    ok: true,
    value: {
      source: { type: "polaris", area: "paper-library", sourceUrl: pageOrigin, topic: title },
      paper: {
        id: paperId,
        title,
        doi: canonicalDoi(identity.doi) || null,
        url: articleUrl,
        pdfUrl: candidates[0]?.url || null,
        sources: ["polaris"],
        polarisTarget: {
          instanceOrigin: origin,
          libraryId,
          paperId,
          searchHitId,
          nonce,
          pmid: cleanText(identity.pmid, 64) || null,
          pmcid: cleanText(identity.pmcid, 64) || null,
          arxivId: cleanText(identity.arxiv_id, 128) || null,
          sourceUrl: candidates[0]?.url || articleUrl,
        },
        candidates,
      },
    },
  };
}

export function validatePolarisBatch(payload, pageOrigin, now = Date.now()) {
  if (!object(payload) || payload.version !== 2) return { ok: false, error: "不支持的 Polaris 批量任务版本" };
  const batchNonce = cleanText(payload.batch_nonce, 200);
  const backendBatchId = payload.backend_batch_id == null ? null : uuid(payload.backend_batch_id);
  const rawPapers = Array.isArray(payload.papers) ? payload.papers : [];
  if (!batchNonce || batchNonce.length < 16 || (payload.backend_batch_id && !backendBatchId)) {
    return { ok: false, error: "Polaris 批量任务缺少必要标识" };
  }
  if (rawPapers.length < 1 || rawPapers.length > 500) {
    return { ok: false, error: "Polaris 批量任务论文数量必须为 1-500" };
  }
  const papers = [];
  const targets = new Set();
  for (const rawPaper of rawPapers) {
    const validated = validatePolarisTask({
      ...rawPaper,
      version: 1,
      instance_origin: payload.instance_origin,
      issued_at: payload.issued_at,
      expires_at: payload.expires_at,
    }, pageOrigin, now);
    if (!validated.ok) return validated;
    const paper = validated.value.paper;
    const targetKey = `${paper.polarisTarget.libraryId}:${paper.polarisTarget.paperId}`;
    if (targets.has(targetKey)) return { ok: false, error: "Polaris 批量任务包含重复论文目标" };
    targets.add(targetKey);
    paper.polarisTarget.backendBatchId = backendBatchId;
    papers.push(paper);
  }
  return {
    ok: true,
    value: {
      batchNonce,
      backendBatchId,
      source: {
        type: "polaris",
        area: "paper-library",
        sourceUrl: pageOrigin,
        topic: `Polaris 批量任务（${papers.length} 篇）`,
        backendBatchId,
      },
      papers,
    },
  };
}
