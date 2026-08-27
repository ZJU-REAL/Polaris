import { ITEM_STATE } from "../shared/constants.js";

const DEFAULT_SELECTED_STATES = new Set([
  ITEM_STATE.PDF_RESPONSE_VERIFIED,
  ITEM_STATE.PDF_CACHED,
  ITEM_STATE.AUTHORIZED,
  ITEM_STATE.QUEUED,
]);

const UNSUCCESSFUL_STATES = new Set([
  ITEM_STATE.LOGIN_REQUIRED,
  ITEM_STATE.MANUAL_REQUIRED,
  ITEM_STATE.BLOCKED,
  ITEM_STATE.NO_ENTITLEMENT,
  ITEM_STATE.FAILED,
  ITEM_STATE.INVALID_RESPONSE,
  ITEM_STATE.VERIFICATION_INCONCLUSIVE,
  ITEM_STATE.QUARANTINED,
]);

export function defaultSelectedItemIds(items) {
  return new Set(items.filter((item) => DEFAULT_SELECTED_STATES.has(item.state)).map((item) => item.id));
}

export function unsuccessfulItemIds(items) {
  return new Set(items.filter((item) => UNSUCCESSFUL_STATES.has(item.state)).map((item) => item.id));
}

export function initialSelectedItemIds(items, origin = null) {
  const yfrPaperIds = Array.isArray(origin?.paperIds) ? new Set(origin.paperIds) : null;
  if (yfrPaperIds?.size) {
    return new Set(items.filter((item) => yfrPaperIds.has(item.yfrPaperId)).map((item) => item.id));
  }
  return defaultSelectedItemIds(items);
}

export function reconcileSelectedItemIds(items, selectedIds, initializeDefaults = false, origin = null) {
  if (initializeDefaults) return initialSelectedItemIds(items, origin);
  const validIds = new Set(items.map((item) => item.id));
  return new Set(Array.from(selectedIds || []).filter((id) => validIds.has(id)));
}
