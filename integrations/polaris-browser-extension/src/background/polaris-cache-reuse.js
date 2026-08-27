import { ITEM_STATE } from "../shared/constants.js";
import { canonicalDoi, canonicalTitle } from "../shared/normalization.js";

export function samePaperIdentity(left, right) {
  const leftDoi = canonicalDoi(left?.doi);
  const rightDoi = canonicalDoi(right?.doi);
  if (leftDoi || rightDoi) return Boolean(leftDoi && rightDoi && leftDoi === rightDoi);
  const leftTitle = canonicalTitle(left?.title);
  const rightTitle = canonicalTitle(right?.title);
  return Boolean(leftTitle && rightTitle && leftTitle === rightTitle);
}

export function reusableCachedItems(incoming, items) {
  return (Array.isArray(items) ? items : [])
    .filter((item) => item?.id !== incoming?.id
      && item?.state === ITEM_STATE.PDF_CACHED
      && item?.cache?.cacheKey
      && samePaperIdentity(incoming, item))
    .sort((left, right) => String(right.updatedAt || "").localeCompare(String(left.updatedAt || "")));
}
