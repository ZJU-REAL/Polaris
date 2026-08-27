import { ITEM_STATE } from "../shared/constants.js";

export function resetItemForDoiReparse(item, now = new Date()) {
  const at = now.toISOString();
  const doiArticleUrl = item.doi ? `https://doi.org/${item.doi}` : item.articleUrl;
  return {
    ...item,
    state: ITEM_STATE.PENDING,
    articleUrl: doiArticleUrl || null,
    publisherKey: null,
    adapter: null,
    identifiers: {},
    candidates: [],
    failedCandidateUrls: [],
    activeCandidateUrl: null,
    manualTabId: null,
    articleTabId: null,
    cache: null,
    cacheProgress: null,
    identityVerification: null,
    identityApproval: null,
    previousFile: item.file || item.previousFile || null,
    file: null,
    retryCount: 0,
    statusReason: item.doi ? "正在从 DOI 重新解析出版社与 PDF 入口" : "正在从文章地址重新解析 PDF 入口",
    updatedAt: at,
    stateHistory: [...(item.stateHistory || []), {
      from: item.state,
      to: ITEM_STATE.PENDING,
      at,
      reason: "doi-reparse",
    }].slice(-40),
  };
}
