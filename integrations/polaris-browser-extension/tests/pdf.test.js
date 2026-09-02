import assert from "node:assert/strict";
import test from "node:test";

import { inspectPdf, validatePdfBytes } from "../src/shared/pdf.js";

function validPdf(size = 2048) {
  const bytes = new Uint8Array(size);
  bytes.set(new TextEncoder().encode("%PDF-1.7\n"));
  return bytes;
}

test("accepts a signed PDF and returns a stable SHA-256", async () => {
  const result = await inspectPdf(validPdf());
  assert.equal(result.byteSize, 2048);
  assert.match(result.sha256, /^[0-9a-f]{64}$/);
  assert.equal((await inspectPdf(validPdf())).sha256, result.sha256);
});

test("rejects HTML masquerading as PDF and truncated files", () => {
  const html = new Uint8Array(2048);
  html.set(new TextEncoder().encode("<!doctype html>"));
  assert.throws(() => validatePdfBytes(html), /%PDF-/);
  assert.throws(() => validatePdfBytes(validPdf(128)), /过小/);
});

test("enforces the caller supplied size limit", () => {
  assert.throws(() => validatePdfBytes(validPdf(4096), 2048), /大小上限/);
});
