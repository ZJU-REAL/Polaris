import { browserPdfCacheKey, getBrowserCachedPdf, hasPdfSignature } from "../background/browser-pdf-cache.js";
import { MESSAGE } from "../shared/constants.js";

const params = new URLSearchParams(location.search);
const taskId = params.get("taskId") || "";
const itemId = params.get("itemId") || "";
const dom = {
  title: document.querySelector("#paper-title"),
  meta: document.querySelector("#paper-meta"),
  status: document.querySelector("#viewer-status"),
  approve: document.querySelector("#approve-pdf"),
  error: document.querySelector("#viewer-error"),
  frame: document.querySelector("#pdf-frame"),
  expectedDoi: document.querySelector("#expected-doi"),
  detectedDoi: document.querySelector("#detected-doi"),
  titleScore: document.querySelector("#title-score"),
  decisionBasis: document.querySelector("#decision-basis"),
};
let objectUrl = null;

async function send(type, payload = {}) {
  const response = await chrome.runtime.sendMessage({ type, ...payload });
  if (!response?.ok) throw new Error(response?.error || "插件后台未完成操作");
  return response;
}

function fail(error) {
  dom.status.textContent = "无法查看";
  dom.status.classList.add("bad");
  dom.error.hidden = false;
  dom.error.textContent = error instanceof Error ? error.message : String(error);
  dom.frame.hidden = true;
  dom.approve.disabled = true;
}

async function load() {
  if (!taskId || !itemId) throw new Error("缓存文献标识不完整");
  const state = await send(MESSAGE.GET_STATE, { taskId });
  const item = state.snapshot?.items?.find((entry) => entry.id === itemId);
  if (!item || item.taskId !== taskId) throw new Error("缓存文献记录不存在");
  const expectedKey = browserPdfCacheKey(taskId, itemId);
  if (item.cache?.cacheKey !== expectedKey) throw new Error("缓存文献标识与任务记录不一致");
  const response = await getBrowserCachedPdf(expectedKey);
  if (!response) throw new Error("浏览器 PDF 缓存已被清理");
  const blob = await response.blob();
  const signature = new Uint8Array(await blob.slice(0, 5).arrayBuffer());
  if (!hasPdfSignature(signature) || blob.size <= 1024) throw new Error("缓存内容不是有效 PDF");

  dom.title.textContent = item.title || "未命名文献";
  dom.meta.textContent = [item.doi || "无 DOI", item.publisher || item.publisherKey || "待识别出版社", `${(blob.size / 1024 / 1024).toFixed(1)} MB`].join(" · ");
  const evidence = item.identityVerification || {};
  dom.expectedDoi.textContent = item.doi || "无";
  dom.detectedDoi.textContent = evidence.detectedDoi || "未检测到";
  dom.titleScore.textContent = Number.isFinite(Number(evidence.titleSimilarity))
    ? `${Math.round(Number(evidence.titleSimilarity) * 100)}%`
    : "未计算";
  dom.decisionBasis.textContent = evidence.decisionBasis || "尚未归档复验";
  const canApprove = ["verification_inconclusive", "quarantined"].includes(item.state)
    && item.identityApproval?.method !== "user";
  dom.status.textContent = item.identityApproval?.method === "user" ? "已人工确认" : "%PDF 签名有效";
  dom.status.classList.add("good");
  dom.approve.disabled = !canApprove;
  dom.approve.textContent = item.identityApproval?.method === "user"
    ? "已人工确认"
    : canApprove ? "人工确认通过" : "等待自动身份校验";
  objectUrl = URL.createObjectURL(blob);
  dom.frame.src = objectUrl;
  dom.frame.hidden = false;
}

dom.approve.addEventListener("click", async () => {
  if (!window.confirm("确认当前缓存 PDF 与所选文献身份一致？此操作会记录为人工确认。")) return;
  dom.approve.disabled = true;
  try {
    await send(MESSAGE.APPROVE_CACHED_PDF, { taskId, itemId });
    dom.status.textContent = "已人工确认";
    dom.approve.textContent = "已人工确认";
  } catch (error) {
    dom.approve.disabled = false;
    fail(error);
  }
});

window.addEventListener("beforeunload", () => {
  if (objectUrl) URL.revokeObjectURL(objectUrl);
});

load().catch(fail);
