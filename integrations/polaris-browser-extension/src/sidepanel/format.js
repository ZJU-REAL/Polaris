import { ITEM_STATE } from "../shared/constants.js";

const LABELS = {
  [ITEM_STATE.PENDING]: "待解析",
  [ITEM_STATE.RESOLVING]: "正在解析",
  [ITEM_STATE.CANDIDATE_REGISTERED]: "PDF入口已发现",
  [ITEM_STATE.PDF_RESPONSE_VERIFIED]: "PDF响应已校验",
  [ITEM_STATE.CACHING]: "正在缓存 PDF",
  [ITEM_STATE.PDF_CACHED]: "PDF已缓存（浏览器）",
  [ITEM_STATE.ARCHIVING]: "正在归档",
  [ITEM_STATE.AUTHORIZED]: "PDF入口已确认",
  [ITEM_STATE.LOGIN_REQUIRED]: "需要机构登录",
  [ITEM_STATE.MANUAL_REQUIRED]: "需要人工验证",
  [ITEM_STATE.NO_ENTITLEMENT]: "当前无权限",
  [ITEM_STATE.BLOCKED]: "已暂停",
  [ITEM_STATE.QUEUED]: "等待下载",
  [ITEM_STATE.DOWNLOADING]: "正在下载",
  [ITEM_STATE.VERIFYING]: "正在校验",
  [ITEM_STATE.COMPLETED]: "已下载（严格校验）",
  [ITEM_STATE.BROWSER_DOWNLOADED]: "浏览器已下载（未严格校验）",
  [ITEM_STATE.VERIFICATION_INCONCLUSIVE]: "有效PDF，身份待核验",
  [ITEM_STATE.INVALID_RESPONSE]: "非PDF响应",
  [ITEM_STATE.QUARANTINED]: "PDF与文献不匹配",
  [ITEM_STATE.ABANDONED]: "已放弃",
  [ITEM_STATE.FAILED]: "失败",
};

export function stateLabel(state) {
  return LABELS[state] || state || "未知";
}

export function stateTone(state) {
  if ([ITEM_STATE.PDF_RESPONSE_VERIFIED, ITEM_STATE.PDF_CACHED, ITEM_STATE.AUTHORIZED, ITEM_STATE.COMPLETED].includes(state)) return "good";
  if ([ITEM_STATE.CANDIDATE_REGISTERED, ITEM_STATE.CACHING, ITEM_STATE.ARCHIVING, ITEM_STATE.BROWSER_DOWNLOADED, ITEM_STATE.LOGIN_REQUIRED, ITEM_STATE.MANUAL_REQUIRED, ITEM_STATE.BLOCKED, ITEM_STATE.VERIFICATION_INCONCLUSIVE].includes(state)) return "warn";
  if ([ITEM_STATE.NO_ENTITLEMENT, ITEM_STATE.INVALID_RESPONSE, ITEM_STATE.QUARANTINED, ITEM_STATE.FAILED].includes(state)) return "bad";
  return "";
}

export function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return bytes ? `${bytes} B` : "-";
}

export function groupByPublisher(items) {
  const groups = new Map();
  for (const item of items || []) {
    const key = item.publisherKey || "generic";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  }
  return groups;
}

export function itemCounts(items) {
  const result = { total: items.length, candidates: 0, manual: 0, completed: 0, failed: 0 };
  for (const item of items) {
    if ([ITEM_STATE.CANDIDATE_REGISTERED, ITEM_STATE.PDF_RESPONSE_VERIFIED, ITEM_STATE.CACHING, ITEM_STATE.PDF_CACHED, ITEM_STATE.AUTHORIZED, ITEM_STATE.QUEUED].includes(item.state)) result.candidates += 1;
    if ([ITEM_STATE.LOGIN_REQUIRED, ITEM_STATE.MANUAL_REQUIRED].includes(item.state)) result.manual += 1;
    if ([ITEM_STATE.COMPLETED, ITEM_STATE.BROWSER_DOWNLOADED].includes(item.state)) result.completed += 1;
    if ([ITEM_STATE.FAILED, ITEM_STATE.INVALID_RESPONSE, ITEM_STATE.QUARANTINED, ITEM_STATE.NO_ENTITLEMENT].includes(item.state)) result.failed += 1;
  }
  return result;
}
