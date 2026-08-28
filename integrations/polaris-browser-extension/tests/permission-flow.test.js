import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("optional host requests happen in the side panel user gesture handlers", async () => {
  const sidePanel = await readFile(path.join(root, "src/sidepanel/app.js"), "utf8");
  const serviceWorker = await readFile(path.join(root, "src/background/service-worker.js"), "utf8");
  assert.match(sidePanel, /await requestOriginPermission\(origin\)/);
  assert.match(sidePanel, /await requestUrlPermission\(select\.value\)/);
  assert.doesNotMatch(serviceWorker, /chrome\.permissions\.request/);
  assert.match(serviceWorker, /await requireUrlPermission\(candidate\.url\)/);
});
