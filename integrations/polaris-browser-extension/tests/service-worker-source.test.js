import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("service worker never reuses a key across different Polaris origins", async () => {
  const source = await readFile(path.join(root, "src/background/service-worker.js"), "utf8");
  assert.match(source, /canReuseExisting = existing && originsMatch/);
  assert.match(source, /suppliedApiKey \|\| \(canReuseExisting \? existing\.apiKey : ""\)/);
  assert.match(source, /chrome\.permissions\.remove/);
});
