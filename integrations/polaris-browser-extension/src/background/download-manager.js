import { DEFAULT_MAX_PDF_BYTES } from "../shared/constants.js";

export function waitForDownload(chromeApi, downloadId, timeoutMs = 10 * 60 * 1000) {
  return new Promise((resolve, reject) => {
    let timer;
    let settled = false;
    const cleanup = () => {
      clearTimeout(timer);
      chromeApi.downloads.onChanged.removeListener(onChanged);
    };
    const finish = (error, download) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (error) reject(error);
      else resolve(download);
    };
    const readCurrent = async () => {
      const matches = await chromeApi.downloads.search({ id: downloadId });
      const download = matches?.[0];
      if (!download) return;
      if (download.state === "complete") finish(null, download);
      else if (download.state === "interrupted") finish(new Error("浏览器下载已中断"));
    };
    const onChanged = (delta) => {
      if (delta.id !== downloadId) return;
      if (delta.error?.current) {
        finish(new Error(`浏览器下载失败: ${delta.error.current}`));
      } else if (delta.state?.current === "complete") void readCurrent();
      else if (delta.state?.current === "interrupted") {
        finish(new Error("浏览器下载已中断"));
      }
    };
    chromeApi.downloads.onChanged.addListener(onChanged);
    timer = setTimeout(() => {
      finish(new Error("浏览器下载超时"));
    }, timeoutMs);
    void readCurrent().catch((error) => finish(error));
  });
}

export function classifyBrowserDownload(download, maxBytes = DEFAULT_MAX_PDF_BYTES) {
  const bytes = Math.max(Number(download?.fileSize || 0), Number(download?.totalBytes || 0));
  const mime = String(download?.mime || "").toLowerCase();
  const filename = String(download?.filename || "");
  if (download?.state !== "complete") return { ok: false, reason: "下载未完成", bytes, mime };
  if (bytes <= 1024) return { ok: false, invalidResponse: true, removeFile: true, reason: "下载文件过小，不是有效论文 PDF", bytes, mime };
  if (bytes > maxBytes) return { ok: false, removeFile: true, reason: "下载文件超过任务大小上限，临时文件已清理", bytes, mime };
  if (mime && !mime.includes("pdf") && mime !== "application/octet-stream") {
    return { ok: false, invalidResponse: true, removeFile: true, reason: `响应类型为 ${mime}，不是 PDF`, bytes, mime };
  }
  if (filename && !filename.toLowerCase().endsWith(".pdf")) {
    return { ok: false, invalidResponse: true, removeFile: true, reason: "下载文件后缀不是 .pdf", bytes, mime };
  }
  return {
    ok: true,
    reason: mime.includes("pdf") ? "浏览器确认 PDF MIME，等待本地文件校验" : "下载完成，等待本地文件签名校验",
    bytes,
    mime,
    mimeVerified: mime.includes("pdf"),
  };
}

export async function removeBrowserDownload(chromeApi, downloadId) {
  try { await chromeApi.downloads.removeFile(downloadId); } catch { /* The file may already have been moved or removed. */ }
  try { await chromeApi.downloads.erase({ id: downloadId }); } catch { /* Download history cleanup is best effort. */ }
}

export async function startBrowserDownload(chromeApi, {
  url,
  filename,
  maxBytes = DEFAULT_MAX_PDF_BYTES,
  conflictAction = "uniquify",
  onCreated = null,
}) {
  const downloadId = await chromeApi.downloads.download({ url, filename, conflictAction, saveAs: false });
  if (typeof downloadId !== "number") throw new Error("浏览器未创建下载任务");
  if (typeof onCreated === "function") await onCreated(downloadId);
  const download = await waitForDownload(chromeApi, downloadId);
  return { downloadId, download, classification: classifyBrowserDownload(download, maxBytes) };
}
