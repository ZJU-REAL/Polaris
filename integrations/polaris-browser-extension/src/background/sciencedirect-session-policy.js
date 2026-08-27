import { ITEM_STATE } from "../shared/constants.js";

export function shouldAttemptScienceDirectCapture(item, articleTabId) {
  if (item?.publisherKey !== "sciencedirect" || typeof articleTabId !== "number") return false;
  return ![ITEM_STATE.NO_ENTITLEMENT, ITEM_STATE.ABANDONED, ITEM_STATE.COMPLETED, ITEM_STATE.PDF_CACHED].includes(item.state);
}
