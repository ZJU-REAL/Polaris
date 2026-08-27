function publisherSlot(item) {
  const publisher = String(item?.publisherKey || "generic");
  return publisher === "generic" ? `generic:${item.id}` : publisher;
}

export function selectPolarisBatchWave(items, maxConcurrency = 2) {
  const selected = [];
  const publishers = new Set();
  for (const item of [...items].sort((left, right) => left.ordinal - right.ordinal)) {
    const publisher = publisherSlot(item);
    if (publishers.has(publisher)) continue;
    selected.push(item);
    publishers.add(publisher);
    if (selected.length >= maxConcurrency) break;
  }
  return selected;
}

export async function runPolarisBatchWaves(items, worker, {
  maxConcurrency = 2,
  shouldStop = () => false,
  onWaveComplete = null,
} = {}) {
  const pending = [...items].sort((left, right) => left.ordinal - right.ordinal);
  const results = [];
  while (pending.length && !shouldStop()) {
    const wave = selectPolarisBatchWave(pending, maxConcurrency);
    const selectedIds = new Set(wave.map((item) => item.id));
    for (let index = pending.length - 1; index >= 0; index -= 1) {
      if (selectedIds.has(pending[index].id)) pending.splice(index, 1);
    }
    results.push(...await Promise.all(wave.map((item) => worker(item))));
    await onWaveComplete?.([...pending]);
  }
  return { results, remaining: pending };
}

