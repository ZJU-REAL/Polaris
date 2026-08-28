import assert from "node:assert/strict";
import test from "node:test";

import { archiveMetadataForItem, archivePdfToPolaris, testPolarisConnection } from "../src/background/api.js";

function pdfResponse() {
  const bytes = new Uint8Array(2048);
  bytes.set(new TextEncoder().encode("%PDF-1.7\n"));
  return new Response(bytes, { headers: { "content-type": "application/pdf" } });
}

function item(paperId, libraryId) {
  return {
    paperId,
    libraryId,
    nonce: `nonce-${paperId}-1234567890`,
    title: `Paper ${paperId}`,
    doi: `10.1000/${paperId}`,
    articleUrl: `https://publisher.example/${paperId}`,
    cache: null,
  };
}

test("connection client uses the user-scoped API key header", async () => {
  let request;
  const result = await testPolarisConnection({
    instanceOrigin: "https://polaris.example.com/path",
    apiKey: "pol_dl_test-value",
    fetchImpl: async (url, init) => {
      request = { url, init };
      return new Response(JSON.stringify({ user_id: "user-1", email: "user@example.com" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    },
  });
  assert.equal(request.url, "https://polaris.example.com/api/download-client/me");
  assert.equal(request.init.headers["X-Polaris-API-Key"], "pol_dl_test-value");
  assert.equal(result.user.email, "user@example.com");
});

test("archive metadata always comes from the individual paper binding", () => {
  const first = archiveMetadataForItem(item("paper-a", "library-a"));
  const second = archiveMetadataForItem(item("paper-b", "library-b"));
  assert.deepEqual([first.library_id, first.paper_id], ["library-a", "paper-a"]);
  assert.deepEqual([second.library_id, second.paper_id], ["library-b", "paper-b"]);
});

test("archive request sends per-paper metadata and a verified checksum", async () => {
  const target = item("paper-a", "library-a");
  let request;
  const response = await archivePdfToPolaris({
    connection: { instanceOrigin: "https://polaris.example.com", apiKey: "pol_dl_test-value" },
    item: target,
    cachedResponse: pdfResponse(),
    fetchImpl: async (url, init) => {
      request = { url, init };
      return new Response(JSON.stringify({ asset_id: "asset-1", content_version_id: "version-1", status: "queued" }), {
        status: 201,
        headers: { "content-type": "application/json" },
      });
    },
  });
  assert.equal(request.url, "https://polaris.example.com/api/download-client/archive");
  assert.match(request.init.headers["X-Polaris-PDF-SHA256"], /^[0-9a-f]{64}$/);
  const metadata = JSON.parse(request.init.body.get("metadata"));
  assert.equal(metadata.library_id, "library-a");
  assert.equal(metadata.paper_id, "paper-a");
  assert.equal(metadata.nonce, target.nonce);
  assert.equal(response.asset_id, "asset-1");
});
