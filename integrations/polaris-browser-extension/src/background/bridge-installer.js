import { BRIDGE_INSTALLER_URL, BUNDLED_BRIDGE_INSTALLER_PATH } from "../shared/constants.js";

export async function resolveBridgeInstallerUrl(chromeApi, fetchImpl = globalThis.fetch) {
  const bundledUrl = chromeApi.runtime?.getURL?.(BUNDLED_BRIDGE_INSTALLER_PATH);
  if (!bundledUrl || typeof fetchImpl !== "function") return BRIDGE_INSTALLER_URL;
  try {
    const response = await fetchImpl(bundledUrl, { method: "HEAD", cache: "no-store" });
    if (response.ok) return bundledUrl;
  } catch {
    // The slim package intentionally falls back to the fixed release asset.
  }
  return BRIDGE_INSTALLER_URL;
}

export async function bridgeInstallerState(chromeApi, downloadId) {
  if (!Number.isInteger(downloadId)) return { available: true, state: "not-started", progress: 0 };
  const [download] = await chromeApi.downloads.search({ id: downloadId });
  if (!download) return { available: true, state: "missing", progress: 0, downloadId };
  const total = Number(download.totalBytes || 0);
  const received = Number(download.bytesReceived || 0);
  return {
    available: true,
    downloadId,
    state: download.state || "unknown",
    progress: total > 0 ? Math.max(0, Math.min(100, Math.round((received / total) * 100))) : 0,
    filename: download.filename || null,
    error: download.error || null,
    canOpen: download.state === "complete",
  };
}

export async function startBridgeInstallerDownload(chromeApi, previousDownloadId = null, fetchImpl = globalThis.fetch) {
  const previous = await bridgeInstallerState(chromeApi, previousDownloadId);
  if (["in_progress", "complete"].includes(previous.state)) return previous;
  const installerUrl = await resolveBridgeInstallerUrl(chromeApi, fetchImpl);
  const downloadId = await chromeApi.downloads.download({
    url: installerUrl,
    filename: "YFRDownloadBridgeSetup.exe",
    conflictAction: "overwrite",
    saveAs: false,
  });
  return bridgeInstallerState(chromeApi, downloadId);
}

async function requireCompleteDownload(chromeApi, downloadId) {
  const state = await bridgeInstallerState(chromeApi, downloadId);
  if (!state.canOpen) throw new Error("本地桥安装包尚未下载完成");
  return state;
}

export async function openBridgeInstaller(chromeApi, downloadId) {
  await requireCompleteDownload(chromeApi, downloadId);
  await chromeApi.downloads.open(downloadId);
  return { ok: true };
}

export async function showBridgeInstaller(chromeApi, downloadId) {
  await requireCompleteDownload(chromeApi, downloadId);
  const shown = await chromeApi.downloads.show(downloadId);
  if (shown === false) throw new Error("浏览器未能显示安装包位置");
  return { ok: true };
}
