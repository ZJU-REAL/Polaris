import { ITEM_STATE } from "../shared/constants.js";

const ACTIVE_STATES = new Set([ITEM_STATE.RESOLVING, ITEM_STATE.DOWNLOADING, ITEM_STATE.VERIFYING]);

export function retryDelayMs(retryCount, baseMs = 4000) {
  const count = Math.max(0, Math.min(6, Number(retryCount) || 0));
  return Math.min(5 * 60 * 1000, baseMs * (2 ** count));
}

export function selectRunnableItems(items, {
  targetStates = [ITEM_STATE.PENDING],
  globalConcurrency = 2,
  publisherConcurrency = 1,
  cooldowns = {},
  now = Date.now(),
} = {}) {
  const active = items.filter((item) => ACTIVE_STATES.has(item.state));
  let slots = Math.max(0, globalConcurrency - active.length);
  if (!slots) return [];
  const publisherActive = new Map();
  for (const item of active) {
    const publisher = item.publisherKey || "generic";
    publisherActive.set(publisher, (publisherActive.get(publisher) || 0) + 1);
  }
  const selected = [];
  for (const item of [...items].sort((left, right) => left.ordinal - right.ordinal)) {
    if (!targetStates.includes(item.state)) continue;
    const publisher = item.publisherKey || "generic";
    if (Number(cooldowns[publisher] || 0) > now) continue;
    const count = publisherActive.get(publisher) || 0;
    if (count >= publisherConcurrency) continue;
    selected.push(item);
    publisherActive.set(publisher, count + 1);
    slots -= 1;
    if (!slots) break;
  }
  return selected;
}
