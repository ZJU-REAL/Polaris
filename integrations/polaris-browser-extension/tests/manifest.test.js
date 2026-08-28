import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

async function filesBelow(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(entries.map((entry) => {
    const target = path.join(directory, entry.name);
    return entry.isDirectory() ? filesBelow(target) : [target];
  }));
  return nested.flat();
}

test("manifest has no install-time host access or forbidden capabilities", async () => {
  const manifest = JSON.parse(await readFile(path.join(root, "manifest.json"), "utf8"));
  assert.equal(manifest.host_permissions, undefined);
  assert.deepEqual(manifest.optional_host_permissions, ["http://*/*", "https://*/*"]);
  assert.deepEqual(manifest.permissions.sort(), ["scripting", "sidePanel", "storage", "unlimitedStorage"].sort());
  for (const forbidden of ["debugger", "nativeMessaging", "webRequest", "declarativeNetRequestWithHostAccess"]) {
    assert.equal(manifest.permissions.includes(forbidden), false);
  }
});

test("core extension contains no excluded integrations or secrets", async () => {
  const files = (await filesBelow(root)).filter((file) => (
    !file.includes(`${path.sep}tests${path.sep}`)
    && (file.endsWith(".js") || file.endsWith("manifest.json"))
  ));
  const source = (await Promise.all(files.map((file) => readFile(file, "utf8")))).join("\n");
  assert.doesNotMatch(source, /\b(?:YFR|SCNet|Zotero|nativeMessaging|debugger)\b/i);
  assert.doesNotMatch(source, /(?:sk-|pol_dl_)[A-Za-z0-9_-]{20,}/);
});
