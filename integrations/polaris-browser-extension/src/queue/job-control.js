import { ITEM_STATE } from "../shared/constants.js";

const SETTLED_STATES = new Set([
  ITEM_STATE.COMPLETED,
  ITEM_STATE.PDF_CACHED,
  ITEM_STATE.BROWSER_DOWNLOADED,
  ITEM_STATE.NO_ENTITLEMENT,
  ITEM_STATE.ABANDONED,
]);

const REGISTRATION_ACCEPTED_STATES = new Set([
  ITEM_STATE.PDF_CACHED,
  ITEM_STATE.COMPLETED,
  ITEM_STATE.ABANDONED,
]);

export function registrationOutcomeAccepted(item) {
  return Boolean(item && REGISTRATION_ACCEPTED_STATES.has(item.state));
}

export function statusAfterQueueEnds(items) {
  if (items.some((item) => item.state === ITEM_STATE.PDF_CACHED)) return "awaiting-archive";
  return items.every((item) => SETTLED_STATES.has(item.state)) ? "completed" : "partial";
}

export function shouldPreserveRegistrationControl(job, scoped) {
  return Boolean(scoped) || ["caching", "archiving", "downloading", "awaiting-user", "queued"].includes(job?.status);
}

export function queuePatchAfterAbandon(job, itemId, items) {
  return queuePatchAfterSettledItem(job, itemId, items);
}

export function queuePatchAfterSettledItem(job, itemId, items) {
  const pendingDownloadItemIds = Array.isArray(job?.pendingDownloadItemIds)
    ? job.pendingDownloadItemIds.filter((id) => id !== itemId)
    : [];
  if (job?.pausedItemId !== itemId) return { pendingDownloadItemIds };
  return {
    status: pendingDownloadItemIds.length ? "queued" : statusAfterQueueEnds(items),
    pausedItemId: null,
    pendingDownloadItemIds,
  };
}

export function registrationItemsForQueue(job, allItems, scopedItems, preserveControl) {
  const pendingIds = Array.isArray(job?.pendingDownloadItemIds) ? job.pendingDownloadItemIds : [];
  if (!preserveControl || job?.queueMode !== "registration" || !pendingIds.length) return scopedItems;
  const byId = new Map(allItems.map((item) => [item.id, item]));
  return pendingIds.map((id) => byId.get(id)).filter(Boolean);
}
