import test from "node:test";
import assert from "node:assert/strict";
import { createPolarisBatch, testPolarisConnection } from "../src/background/polaris-client.js";

const target = {
  articleUrl: "https://publisher.example/article",
  candidates: [{ url: "https://publisher.example/article.pdf", source: "oa" }],
  polarisTarget: {
    instanceOrigin: "https://polaris.example",
    libraryId: "00000000-0000-4000-8000-000000000001",
    paperId: "00000000-0000-4000-8000-000000000002",
  },
};

test("tests a Polaris API key connection", async () => {
  const response = await testPolarisConnection({
    instanceOrigin: "https://polaris.example",
    apiKey: "pol_dl_test-key",
    fetchImpl: async (url, options) => {
      assert.equal(url, "https://polaris.example/api/download-client/me");
      assert.equal(options.headers["X-Polaris-API-Key"], "pol_dl_test-key");
      return new Response(JSON.stringify({ user_id: "user-1" }), { status: 200 });
    },
  });
  assert.equal(response.user.user_id, "user-1");
});

test("creates exactly one backend batch for multiple paper targets", async () => {
  let calls = 0;
  const result = await createPolarisBatch({
    instanceOrigin: "https://polaris.example",
    apiKey: "pol_dl_test-key",
    papers: [target, { ...target, polarisTarget: { ...target.polarisTarget, paperId: "00000000-0000-4000-8000-000000000003" } }],
    fetchImpl: async (url, options) => {
      calls += 1;
      assert.equal(url, "https://polaris.example/api/download-batches");
      const payload = JSON.parse(options.body);
      assert.equal(payload.targets.length, 2);
      return new Response(JSON.stringify({ id: "batch-1", item_count: 2, status: "queued" }), { status: 200 });
    },
  });
  assert.equal(calls, 1);
  assert.equal(result.id, "batch-1");
});
