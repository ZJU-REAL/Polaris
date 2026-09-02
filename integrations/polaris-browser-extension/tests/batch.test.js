import assert from "node:assert/strict";
import test from "node:test";

import { createLocalTask, validateDownloadBatch } from "../src/shared/batch.js";

const NOW = Date.parse("2026-08-29T08:00:00Z");
const ORIGIN = "https://polaris.example.com";

function payload() {
  return {
    version: 2,
    instance_origin: ORIGIN,
    issued_at: "2026-08-29T07:59:30Z",
    expires_at: "2026-08-29T08:04:30Z",
    batch_nonce: "batch-nonce-1234567890",
    backend_batch_id: "3d3bfa16-dacb-4bfa-a93e-83f33150e669",
    papers: [
      {
        nonce: "paper-nonce-1234567890",
        library_id: "09d24444-0451-4541-8b7e-3f28fafda2d2",
        paper_id: "03bbcc49-1731-4d47-814f-dbf173f5ed85",
        identity: { title: "First paper", doi: "10.1000/first" },
        article_url: "https://publisher.example/first",
        pdf_candidates: [{ url: "https://cdn.example/first.pdf", source: "oa" }],
      },
      {
        nonce: "paper-nonce-0987654321",
        library_id: "09d24444-0451-4541-8b7e-3f28fafda2d2",
        paper_id: "db81be8d-c58c-4609-91d8-1bb75d00959b",
        identity: { title: "Second paper", pmid: "12345" },
        article_url: "https://publisher.example/second",
        pdf_candidates: ["https://cdn.example/second.pdf"],
      },
    ],
  };
}

test("validates one batch while preserving independent paper bindings", () => {
  const result = validateDownloadBatch(payload(), ORIGIN, NOW);
  assert.equal(result.ok, true);
  assert.equal(result.value.papers.length, 2);
  assert.equal(result.value.papers[0].paperId, "03bbcc49-1731-4d47-814f-dbf173f5ed85");
  assert.equal(result.value.papers[1].paperId, "db81be8d-c58c-4609-91d8-1bb75d00959b");

  let counter = 0;
  const task = createLocalTask(result.value, () => `local-id-${counter += 1}`, new Date(NOW));
  assert.equal(task.id, "local-id-1");
  assert.equal(task.items.length, 2);
  assert.equal(task.items[0].taskId, task.id);
  assert.notEqual(task.items[0].paperId, task.items[1].paperId);
  assert.equal(task.items[1].pmid, "12345");
});

test("rejects expired, cross-origin, and duplicate-bound batches", () => {
  assert.match(validateDownloadBatch(payload(), ORIGIN, NOW + 10 * 60_000).error, /过期/);
  assert.match(validateDownloadBatch(payload(), "https://other.example.com", NOW).error, /不一致/);
  const duplicate = payload();
  duplicate.papers[1].paper_id = duplicate.papers[0].paper_id;
  assert.match(validateDownloadBatch(duplicate, ORIGIN, NOW).error, /重复/);
});

test("rejects malformed PDF candidate URLs without rejecting the paper", () => {
  const value = payload();
  value.papers[0].pdf_candidates = ["javascript:alert(1)", "https://cdn.example/paper.pdf"];
  const result = validateDownloadBatch(value, ORIGIN, NOW);
  assert.equal(result.ok, true);
  assert.deepEqual(result.value.papers[0].candidates.map((entry) => entry.url), ["https://cdn.example/paper.pdf"]);
});
