import assert from "node:assert/strict";
import test from "node:test";

import { cachePdfBytes, fetchAndCachePdf, getCachedPdf, pdfCacheKey } from "../src/background/cache.js";

function validPdf() {
  const bytes = new Uint8Array(2048);
  bytes.set(new TextEncoder().encode("%PDF-1.7\n"));
  return bytes;
}

function memoryCacheStorage() {
  const entries = new Map();
  return {
    entries,
    async open() {
      return {
        async put(key, value) { entries.set(key, value); },
        async match(key) { return entries.get(key)?.clone() || null; },
        async delete(key) { return entries.delete(key); },
      };
    },
  };
}

test("stores verified PDF bytes under a task and item scoped cache key", async () => {
  const storage = memoryCacheStorage();
  const result = await cachePdfBytes({
    taskId: "task-a",
    itemId: "item-b",
    value: validPdf(),
    sourceUrl: "https://cdn.example/paper.pdf",
    cacheStorage: storage,
  });
  assert.equal(result.cacheKey, pdfCacheKey("task-a", "item-b"));
  assert.equal(result.byteSize, 2048);
  assert.match(result.sha256, /^[0-9a-f]{64}$/);
  const cached = await getCachedPdf(result.cacheKey, storage);
  assert.equal(cached.headers.get("content-type"), "application/pdf");
  assert.equal((await cached.arrayBuffer()).byteLength, 2048);
});

test("candidate fetch rejects an HTML response before it reaches cache", async () => {
  const storage = memoryCacheStorage();
  await assert.rejects(fetchAndCachePdf({
    taskId: "task-a",
    itemId: "item-b",
    url: "https://publisher.example/download",
    fetchImpl: async () => new Response("<!doctype html>".padEnd(2048, " "), { status: 200 }),
    cacheStorage: storage,
  }), /%PDF-/);
  assert.equal(storage.entries.size, 0);
});
