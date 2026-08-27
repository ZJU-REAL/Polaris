import { MAX_IMPORT_ITEMS, SCHEMA_VERSION } from "./constants.js";
import { cleanText } from "./normalization.js";
import { isAllowedYfrOrigin, parseSafeHttpUrl, sanitizeImportedHttpUrl } from "./url-security.js";

function plainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function sourceAreaFor(payloadSource) {
  const supplied = cleanText(payloadSource.area, 40);
  if (supplied) return supplied;
  if (payloadSource.type === "yfr-search") return "literature-search";
  return payloadSource.exclusivePage ? "exclusive-review" : "daily-review";
}

function sourcePathMatchesArea(sourceUrl, area) {
  const path = new URL(sourceUrl).pathname.toLowerCase();
  if (area === "literature-search") return path === "/literature-search" || path.startsWith("/literature-search/");
  if (area === "exclusive-review") return path === "/admin/exclusive-review" || path.startsWith("/admin/exclusive-review/");
  return path === "/daily-review" || path.startsWith("/daily-review/");
}

export function validateYfrSelectionPayload(payload, pageOrigin) {
  if (!isAllowedYfrOrigin(pageOrigin)) return { ok: false, error: "不允许的 YFR 页面来源" };
  if (!plainObject(payload) || payload.version !== SCHEMA_VERSION) return { ok: false, error: "不支持的任务数据版本" };
  if (!plainObject(payload.source) || !["yfr-search", "yfr-review"].includes(payload.source.type)) {
    return { ok: false, error: "无效的任务来源" };
  }
  if (!Array.isArray(payload.papers) || payload.papers.length < 1 || payload.papers.length > MAX_IMPORT_ITEMS) {
    return { ok: false, error: `文献数量必须为 1-${MAX_IMPORT_ITEMS}` };
  }
  const sourceUrl = parseSafeHttpUrl(payload.source.sourceUrl, { allowLocalDevelopment: true });
  if (!sourceUrl || new URL(sourceUrl).origin !== pageOrigin) return { ok: false, error: "任务页面地址与当前页面不一致" };
  const area = sourceAreaFor(payload.source);
  const allowedAreas = payload.source.type === "yfr-search"
    ? ["literature-search"]
    : ["daily-review", "exclusive-review"];
  if (!allowedAreas.includes(area) || !sourcePathMatchesArea(sourceUrl, area)) {
    return { ok: false, error: "任务来源区域与页面路径不一致" };
  }

  const papers = [];
  const seenIds = new Set();
  for (const raw of payload.papers) {
    if (!plainObject(raw)) return { ok: false, error: "文献数据格式无效" };
    const id = cleanText(raw.id, 160);
    const title = cleanText(raw.title, 1000);
    if (!id || !title) return { ok: false, error: "文献缺少编号或标题" };
    if (seenIds.has(id)) return { ok: false, error: `YFR 文献编号重复：${id}` };
    seenIds.add(id);
    papers.push({
      id,
      title,
      authors: Array.isArray(raw.authors) ? raw.authors.slice(0, 50).map((item) => cleanText(item, 240)).filter(Boolean) : [],
      year: Number.isInteger(raw.year) ? raw.year : null,
      venue: cleanText(raw.venue, 500) || null,
      publisher: cleanText(raw.publisher, 500) || null,
      doi: cleanText(raw.doi, 512) || null,
      url: sanitizeImportedHttpUrl(raw.url, { expectedYfrOrigin: pageOrigin }),
      pdfUrl: sanitizeImportedHttpUrl(raw.pdfUrl, { expectedYfrOrigin: pageOrigin, preserveYfrPdfAccessKey: true }),
      pdfRemoteUrl: sanitizeImportedHttpUrl(raw.pdfRemoteUrl, { expectedYfrOrigin: pageOrigin, preserveYfrPdfAccessKey: true }),
      pdfSource: cleanText(raw.pdfSource, 160) || null,
      pdfAvailable: Boolean(raw.pdfAvailable),
      pdfCached: Boolean(raw.pdfCached),
      sources: Array.isArray(raw.sources) ? raw.sources.slice(0, 24).map((item) => cleanText(item, 160)).filter(Boolean) : [],
    });
  }
  const selectedCount = Number.isInteger(payload.selectedCount) ? payload.selectedCount : papers.length;
  if (selectedCount !== papers.length) {
    return { ok: false, error: `勾选数量 ${selectedCount} 与传入文献数量 ${papers.length} 不一致` };
  }
  const paperIds = Array.isArray(payload.paperIds)
    ? payload.paperIds.map((item) => cleanText(item, 160))
    : papers.map((paper) => paper.id);
  if (paperIds.length !== papers.length || new Set(paperIds).size !== paperIds.length) {
    return { ok: false, error: "文献编号清单存在缺失或重复" };
  }
  if (paperIds.some((id, index) => id !== papers[index].id)) {
    return { ok: false, error: "文献编号清单与文献顺序不一致" };
  }
  return {
    ok: true,
    value: {
      version: SCHEMA_VERSION,
      source: {
        type: payload.source.type,
        area,
        searchId: cleanText(payload.source.searchId, 160) || null,
        runId: cleanText(payload.source.runId, 160) || null,
        topic: cleanText(payload.source.topic, 500) || null,
        exclusivePage: area === "exclusive-review",
        sourceUrl,
      },
      selectedCount,
      paperIds,
      papers,
      createdAt: cleanText(payload.createdAt, 80) || new Date().toISOString(),
    },
  };
}
