import { normalizePolarisOrigin, permissionPatternForUrl } from "../shared/origin.js";

const SCRIPT_ID = "polaris-download-bridge";

export async function requestOriginPermission(origin) {
  const pattern = permissionPatternForUrl(normalizePolarisOrigin(origin));
  const granted = await chrome.permissions.request({ origins: [pattern] });
  if (!granted) throw new Error("需要授权访问该 Polaris 地址");
  return pattern;
}

export async function requestUrlPermission(url) {
  const pattern = permissionPatternForUrl(url);
  const granted = await chrome.permissions.request({ origins: [pattern] });
  if (!granted) throw new Error("未授权访问该 PDF 来源");
  return pattern;
}

export async function requireUrlPermission(url) {
  const pattern = permissionPatternForUrl(url);
  const granted = await chrome.permissions.contains({ origins: [pattern] });
  if (!granted) throw new Error("未授权访问该 PDF 来源，请重新点击下载");
  return pattern;
}

export async function registerPolarisBridge(origin) {
  const normalized = normalizePolarisOrigin(origin);
  const pattern = permissionPatternForUrl(normalized);
  const allowed = await chrome.permissions.contains({ origins: [pattern] });
  if (!allowed) return false;
  const registrations = await chrome.scripting.getRegisteredContentScripts({ ids: [SCRIPT_ID] });
  if (registrations.length) await chrome.scripting.unregisterContentScripts({ ids: [SCRIPT_ID] });
  await chrome.scripting.registerContentScripts([{
    id: SCRIPT_ID,
    matches: [pattern],
    js: ["src/content/polaris-bridge.js"],
    runAt: "document_idle",
    persistAcrossSessions: true,
  }]);
  return true;
}
