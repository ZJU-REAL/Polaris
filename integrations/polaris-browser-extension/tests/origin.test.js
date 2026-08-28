import assert from "node:assert/strict";
import test from "node:test";

import {
  normalizeHttpUrl,
  normalizePolarisOrigin,
  originsMatch,
  permissionPatternForUrl,
} from "../src/shared/origin.js";

test("normalizes public HTTPS and local development origins", () => {
  assert.equal(normalizePolarisOrigin("polaris.example.com/path"), "https://polaris.example.com");
  assert.equal(normalizePolarisOrigin("localhost:8000/libraries"), "http://localhost:8000");
  assert.equal(originsMatch("https://polaris.example.com/a", "https://polaris.example.com/b"), true);
});

test("rejects public HTTP and embedded credentials", () => {
  assert.throws(() => normalizePolarisOrigin("http://polaris.example.com"), /HTTPS/);
  assert.throws(() => normalizePolarisOrigin("https://user:secret@polaris.example.com"), /安全/);
});

test("derives an exact-host optional permission pattern", () => {
  assert.equal(permissionPatternForUrl("https://polaris.example.com:8443/api"), "https://polaris.example.com/*");
  assert.equal(normalizeHttpUrl("javascript:alert(1)"), null);
});
