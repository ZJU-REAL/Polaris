import { ITEM_STATE } from "../shared/constants.js";

export function requiresPublisherNavigation(candidate) {
  return Boolean(candidate?.sessionNavigationOnly);
}

export function candidateCanUseBrowserCache(item, candidate) {
  if (!candidate) return false;
  if (item?.publisherKey === "sciencedirect") return false;
  if (requiresPublisherNavigation(candidate)) return false;
  if (candidate.kind === "open-access" && !candidate.browserNavigationPreferred) return true;
  if ([ITEM_STATE.PDF_RESPONSE_VERIFIED, ITEM_STATE.AUTHORIZED, ITEM_STATE.QUEUED].includes(item?.state)) return true;
  return false;
}

export function stagedDownloadFilename(job, item) {
  if (!job?.taskCode || !item?.plannedFilename) throw new Error("PDF 缓存路径信息不完整");
  return `YFR/staging/${job.taskCode}/${item.plannedFilename}`;
}
