import { normalizePolarisOrigin } from "../shared/polaris.js";

function assertPolarisApiKey(apiKey) {
  if (!String(apiKey || "").startsWith("pol_dl_")) throw new Error("Polaris API Key 格式无效");
}

export async function testPolarisConnection({ instanceOrigin, apiKey, fetchImpl = fetch }) {
  const origin = normalizePolarisOrigin(instanceOrigin);
  assertPolarisApiKey(apiKey);
  const response = await fetchImpl(`${origin}/api/download-client/me`, {
    headers: { "X-Polaris-API-Key": apiKey },
  });
  if (!response.ok) throw new Error(response.status === 401 ? "Polaris API Key 无效或已失效" : `连接失败（HTTP ${response.status}）`);
  return { origin, user: await response.json() };
}

export async function createPolarisBatch({ instanceOrigin, apiKey, papers, fetchImpl = fetch }) {
  const origin = normalizePolarisOrigin(instanceOrigin);
  assertPolarisApiKey(apiKey);
  if (!Array.isArray(papers) || papers.length < 1 || papers.length > 500) {
    throw new Error("Polaris 批量目标数量必须为 1-500");
  }
  const targets = papers.map((paper) => {
    const target = paper?.polarisTarget;
    if (!target?.libraryId || !target?.paperId) throw new Error("Polaris 论文缺少库或论文绑定");
    if (target.instanceOrigin && normalizePolarisOrigin(target.instanceOrigin) !== origin) {
      throw new Error("批量任务包含不同 Polaris 实例");
    }
    return {
      library_id: target.libraryId,
      paper_id: target.paperId,
      article_url: paper.articleUrl || null,
      pdf_candidates: Array.isArray(paper.candidates) ? paper.candidates.slice(0, 40) : [],
    };
  });
  const response = await fetchImpl(`${origin}/api/download-batches`, {
    method: "POST",
    headers: { "X-Polaris-API-Key": apiKey, "Content-Type": "application/json" },
    body: JSON.stringify({ targets }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Polaris 批量任务创建失败（HTTP ${response.status}）`);
  return { ...payload, origin };
}

function connectionUserIdentity(user) {
  return String(user?.user_id || user?.id || user?.email || "").trim().toLowerCase();
}

function isLoopbackOrigin(origin) {
  try {
    return ["127.0.0.1", "localhost"].includes(new URL(origin).hostname);
  } catch {
    return false;
  }
}

export async function rebindLoopbackPolarisConnection({
  connection,
  instanceOrigin,
  fetchImpl = fetch,
}) {
  const nextOrigin = normalizePolarisOrigin(instanceOrigin);
  if (nextOrigin === connection?.instanceOrigin) return connection;
  if (!isLoopbackOrigin(connection?.instanceOrigin) || !isLoopbackOrigin(nextOrigin)) {
    throw new Error("SCNet 凭据绑定的 Polaris 实例不一致");
  }

  const verified = await testPolarisConnection({
    instanceOrigin: nextOrigin,
    apiKey: connection?.apiKey,
    fetchImpl,
  });
  const previousIdentity = connectionUserIdentity(connection?.user);
  const verifiedIdentity = connectionUserIdentity(verified.user);
  if (!previousIdentity || !verifiedIdentity || previousIdentity !== verifiedIdentity) {
    throw new Error("新端口返回的 Polaris 用户与原连接不一致");
  }
  return {
    ...connection,
    instanceOrigin: verified.origin,
    user: verified.user,
    updatedAt: new Date().toISOString(),
  };
}

export async function syncScnetSnapshots({
  instanceOrigin,
  apiKey,
  credentialId,
  snapshots,
  nonce = null,
  accountFingerprint = null,
  purpose = "planning",
  fetchImpl = fetch,
}) {
  const origin = normalizePolarisOrigin(instanceOrigin);
  assertPolarisApiKey(apiKey);
  if (!/^[0-9a-f-]{16,64}$/i.test(String(credentialId || ""))) {
    throw new Error("请先绑定 Polaris 的 SCNet 凭据");
  }
  if (!Array.isArray(snapshots) || snapshots.length < 1 || snapshots.length > 500) {
    throw new Error("SCNet 模板快照为空或数量超限");
  }
  if (nonce) {
    const sessionResponse = await fetchImpl(`${origin}/api/scnet/browser-discovery/sessions`, {
      method: "POST",
      headers: { "X-Polaris-API-Key": apiKey, "Content-Type": "application/json" },
      body: JSON.stringify({ credential_id: credentialId, nonce, account_fingerprint: accountFingerprint }),
    });
    const sessionPayload = await sessionResponse.json().catch(() => ({}));
    if (!sessionResponse.ok) {
      throw new Error(sessionPayload.detail || `SCNet 发现会话创建失败（HTTP ${sessionResponse.status}）`);
    }
    const capabilities = snapshots.map((item) => ({
      template_id: item.template_id,
      region_key: item.region_key,
      status: item.status,
      evidence: item.evidence,
    }));
    const snapshotResponse = await fetchImpl(
      `${origin}/api/scnet/browser-discovery/sessions/${encodeURIComponent(sessionPayload.id)}/snapshot?nonce=${encodeURIComponent(nonce)}`,
      {
        method: "POST",
        headers: { "X-Polaris-API-Key": apiKey, "Content-Type": "application/json" },
        body: JSON.stringify({
          schema_version: 1,
          account_fingerprint: accountFingerprint,
          region_key: snapshots.find((item) => item.region_key)?.region_key || null,
          capabilities,
          purpose,
        }),
      },
    );
    const snapshotPayload = await snapshotResponse.json().catch(() => ({}));
    if (!snapshotResponse.ok) {
      throw new Error(snapshotPayload.detail || `SCNet 能力快照同步失败（HTTP ${snapshotResponse.status}）`);
    }
    return {
      ok: true,
      origin,
      count: capabilities.length,
      discoverySessionId: sessionPayload.id,
      snapshotId: snapshotPayload.id,
      source: "browser_discovery",
    };
  }
  const response = await fetchImpl(
    `${origin}/api/scnet/credentials/${encodeURIComponent(credentialId)}/templates/browser-snapshot`,
    {
      method: "POST",
      headers: {
        "X-Polaris-API-Key": apiKey,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ snapshots }),
    },
  );
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || `SCNet 模板快照同步失败（HTTP ${response.status}）`);
  }
  return { ok: true, origin, count: Array.isArray(payload) ? payload.length : snapshots.length };
}

export async function archivePdfToPolaris({ response, item, connection, fetchImpl = fetch }) {
  const target = item.polarisTarget;
  if (!target) throw new Error("文献缺少 Polaris 归档目标");
  const origin = normalizePolarisOrigin(connection?.instanceOrigin);
  if (origin !== target.instanceOrigin) throw new Error("当前连接的 Polaris 实例与任务来源不一致");
  if (!String(connection?.apiKey || "").startsWith("pol_dl_")) throw new Error("请先配置 Polaris API Key");
  const blob = await response.blob();
  const digest = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  const checksum = Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("");
  const form = new FormData();
  form.set("metadata", JSON.stringify({
    library_id: target.libraryId,
    paper_id: target.paperId,
    search_hit_id: target.searchHitId,
    batch_id: target.backendBatchId || null,
    nonce: target.nonce,
    doi: item.doi,
    pmid: target.pmid,
    pmcid: target.pmcid,
    arxiv_id: target.arxivId,
    title: item.title,
    source_url: target.sourceUrl,
  }));
  form.set("pdf", blob, item.plannedFilename || "paper.pdf");
  const result = await fetchImpl(`${origin}/api/download-client/archive`, {
    method: "POST",
    headers: { "X-Polaris-API-Key": connection.apiKey, "X-Polaris-PDF-SHA256": checksum },
    body: form,
  });
  const payload = await result.json().catch(() => ({}));
  if (!result.ok && String(payload.detail || '') === 'LIBRARY_PAPER_NOT_FOUND') {
    throw new Error('论文尚未筛选进入该文献库，请先入库后重试归档；浏览器缓存已保留');
  }
  if (!result.ok) throw new Error(payload.detail || `Polaris 归档失败（HTTP ${result.status}）`);
  return payload;
}
