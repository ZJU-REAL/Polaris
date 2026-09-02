import { normalizePolarisOrigin } from "../shared/origin.js";
import { inspectPdf } from "../shared/pdf.js";

function assertApiKey(apiKey) {
  if (!String(apiKey || "").startsWith("pol_dl_")) throw new Error("Polaris API Key 格式无效");
}

async function responseError(response, fallback) {
  const payload = await response.json().catch(() => ({}));
  if (response.status === 401) return "Polaris API Key 无效或已失效";
  if (response.status === 403) return "当前用户无权管理目标文献库";
  return String(payload.detail || fallback);
}

export async function testPolarisConnection({ instanceOrigin, apiKey, fetchImpl = fetch }) {
  const origin = normalizePolarisOrigin(instanceOrigin);
  assertApiKey(apiKey);
  const response = await fetchImpl(`${origin}/api/download-client/me`, {
    headers: { "X-Polaris-API-Key": apiKey },
  });
  if (!response.ok) throw new Error(await responseError(response, `连接失败（HTTP ${response.status}）`));
  return { origin, user: await response.json() };
}

export function archiveMetadataForItem(item) {
  if (!item?.libraryId || !item?.paperId || !item?.nonce || !item?.title) {
    throw new Error("论文缺少独立归档绑定");
  }
  return {
    library_id: item.libraryId,
    paper_id: item.paperId,
    nonce: item.nonce,
    doi: item.doi || null,
    pmid: item.pmid || null,
    pmcid: item.pmcid || null,
    arxiv_id: item.arxivId || null,
    title: item.title,
    source_url: item.cache?.sourceUrl || item.articleUrl || null,
  };
}

export async function archivePdfToPolaris({
  connection,
  item,
  cachedResponse,
  fetchImpl = fetch,
}) {
  const origin = normalizePolarisOrigin(connection?.instanceOrigin);
  assertApiKey(connection?.apiKey);
  if (!cachedResponse) throw new Error("浏览器缓存中没有该论文 PDF");
  const bytes = new Uint8Array(await cachedResponse.arrayBuffer());
  const inspected = await inspectPdf(bytes);
  if (item.cache?.sha256 && inspected.sha256 !== item.cache.sha256) {
    throw new Error("PDF 浏览器缓存校验失败");
  }
  const form = new FormData();
  form.set("metadata", JSON.stringify(archiveMetadataForItem(item)));
  form.set("pdf", new Blob([bytes], { type: "application/pdf" }), `${item.paperId}.pdf`);
  const response = await fetchImpl(`${origin}/api/download-client/archive`, {
    method: "POST",
    headers: {
      "X-Polaris-API-Key": connection.apiKey,
      "X-Polaris-PDF-SHA256": inspected.sha256,
    },
    body: form,
  });
  if (!response.ok) throw new Error(await responseError(response, `归档失败（HTTP ${response.status}）`));
  return response.json();
}
