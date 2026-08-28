const SIGNATURE = new TextEncoder().encode("%PDF-");

export const MAX_PDF_BYTES = 150 * 1024 * 1024;
export const MIN_PDF_BYTES = 1024;

export function toUint8Array(value) {
  if (value instanceof Uint8Array) return value;
  if (value instanceof ArrayBuffer) return new Uint8Array(value);
  if (ArrayBuffer.isView(value)) return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  throw new Error("PDF 数据格式无效");
}

export function validatePdfBytes(value, maxBytes = MAX_PDF_BYTES) {
  const bytes = toUint8Array(value);
  if (bytes.byteLength < MIN_PDF_BYTES) throw new Error("PDF 文件过小或内容不完整");
  if (bytes.byteLength > maxBytes) throw new Error("PDF 超过 150 MB 大小上限");
  for (let index = 0; index < SIGNATURE.length; index += 1) {
    if (bytes[index] !== SIGNATURE[index]) throw new Error("文件缺少 %PDF- 签名");
  }
  return bytes;
}

export async function sha256Hex(value, subtle = crypto.subtle) {
  const bytes = toUint8Array(value);
  const source = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
  const digest = await subtle.digest("SHA-256", source);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export async function inspectPdf(value, options = {}) {
  const bytes = validatePdfBytes(value, options.maxBytes);
  return {
    bytes,
    byteSize: bytes.byteLength,
    sha256: await sha256Hex(bytes, options.subtle),
  };
}
