const STRONG_BREAKS = new Set(['\n', '。', '！', '？', '!', '?', '；', ';']);
const SOFT_BREAKS = new Set(['，', ',', '、', ' ']);

function preferredBreak(text: string, start: number, limit: number): number {
  const minimum = start + Math.floor((limit - start) * 0.55);
  for (let index = limit; index > minimum; index -= 1) {
    if (STRONG_BREAKS.has(text[index - 1] ?? '')) return index;
  }
  for (let index = limit; index > minimum; index -= 1) {
    if (SOFT_BREAKS.has(text[index - 1] ?? '')) return index;
  }
  return limit;
}

/**
 * Split read-aloud content into provider-sized requests.
 *
 * Sentence endings are preferred, but an unbroken identifier or URL is hard
 * split so every returned part always respects the upstream limit.
 */
export function splitSpeechText(text: string, maxChars: number): string[] {
  const clean = text.trim();
  if (!clean) return [];
  const size = Math.max(1, Math.floor(maxChars));
  const parts: string[] = [];
  let start = 0;

  while (start < clean.length) {
    const hardLimit = Math.min(clean.length, start + size);
    const end = hardLimit < clean.length
      ? preferredBreak(clean, start, hardLimit)
      : hardLimit;
    const part = clean.slice(start, end).trim();
    if (part) parts.push(part);
    start = end;
    while (start < clean.length && /\s/.test(clean[start] ?? '')) start += 1;
  }
  return parts;
}

/** Decode arbitrary network chunks of mono signed 16-bit little-endian PCM. */
export function pcm16LeToFloat32(
  bytes: Uint8Array,
  leadingByte: number | null = null,
): { samples: Float32Array; trailingByte: number | null } {
  const totalBytes = bytes.byteLength + (leadingByte === null ? 0 : 1);
  const evenBytes = totalBytes - (totalBytes % 2);
  const samples = new Float32Array(evenBytes / 2);
  let sampleIndex = 0;
  let byteIndex = 0;

  if (bytes.byteLength === 0) {
    return { samples, trailingByte: leadingByte };
  }

  if (leadingByte !== null && bytes.byteLength > 0) {
    const value = (leadingByte | (bytes[0]! << 8)) << 16 >> 16;
    samples[sampleIndex] = value / (value < 0 ? 32_768 : 32_767);
    sampleIndex += 1;
    byteIndex = 1;
  }
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  while (sampleIndex < samples.length && byteIndex + 1 < bytes.byteLength) {
    const value = view.getInt16(byteIndex, true);
    samples[sampleIndex] = value / (value < 0 ? 32_768 : 32_767);
    sampleIndex += 1;
    byteIndex += 2;
  }
  return {
    samples,
    trailingByte: byteIndex < bytes.byteLength ? bytes[bytes.byteLength - 1]! : null,
  };
}
