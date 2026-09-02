export interface PolarisExtensionPaper {
  libraryId: string;
  paperId: string;
  title: string;
  doi?: string | null;
  articleUrl?: string | null;
  pdfCandidates?: unknown[] | null;
}

export interface PolarisExtensionBatch {
  batchId: string;
  papers: PolarisExtensionPaper[];
}

export interface PolarisExtensionBatchItemState {
  library_id: string;
  paper_id: string;
  status: string;
}

type ExtensionAck = { ok?: boolean; requestId?: string };

function paperBinding(libraryId: string, paperId: string) {
  return `${libraryId}:${paperId}`;
}

/**
 * 后端是下载资格的最终判定方。只把仍处于 queued 的独立论文绑定交给扩展，
 * 避免已有 PDF 的 skipped 条目进入浏览器任务，也不依赖数组位置匹配论文。
 */
export function dispatchablePolarisExtensionPapers(
  papers: PolarisExtensionPaper[],
  items: PolarisExtensionBatchItemState[],
): PolarisExtensionPaper[] {
  const queued = new Set(
    items
      .filter((item) => item.status === 'queued')
      .map((item) => paperBinding(item.library_id, item.paper_id)),
  );
  return papers.filter((paper) => queued.has(paperBinding(paper.libraryId, paper.paperId)));
}

export function createPolarisExtensionPayload(
  input: PolarisExtensionBatch,
  instanceOrigin: string,
  issuedAt = new Date(),
  randomUUID: () => string = () => crypto.randomUUID(),
) {
  const batchNonce = `${randomUUID()}-${randomUUID()}`;
  return {
    version: 2,
    instance_origin: instanceOrigin,
    issued_at: issuedAt.toISOString(),
    expires_at: new Date(issuedAt.getTime() + 10 * 60_000).toISOString(),
    batch_nonce: batchNonce,
    backend_batch_id: input.batchId,
    papers: input.papers.map((paper) => ({
      nonce: `${randomUUID()}-${randomUUID()}`,
      library_id: paper.libraryId,
      paper_id: paper.paperId,
      identity: { title: paper.title, doi: paper.doi ?? null },
      article_url: paper.articleUrl ?? null,
      pdf_candidates: Array.isArray(paper.pdfCandidates) ? paper.pdfCandidates : [],
    })),
  };
}

/**
 * 将一个后端下载批次交给浏览器扩展。后端批次负责持久化与绑定，页面事件只负责
 * 即时唤醒扩展；即使扩展未确认，批次仍可由扩展稍后通过用户 API Key 认领。
 */
export function dispatchPolarisExtensionBatch(input: PolarisExtensionBatch): Promise<boolean> {
  if (typeof document === 'undefined' || typeof window === 'undefined') return Promise.resolve(false);
  const payload = createPolarisExtensionPayload(input, window.location.origin);
  return new Promise((resolve) => {
    let settled = false;
    const eventName = 'polaris:download-batch-ack:v2';
    const finish = (ok: boolean) => {
      if (settled) return;
      settled = true;
      document.removeEventListener(eventName, onAck as EventListener);
      window.clearTimeout(timer);
      resolve(ok);
    };
    const onAck = (event: Event) => {
      const detail = (event as CustomEvent<ExtensionAck>).detail;
      if (detail?.requestId !== payload.batch_nonce) return;
      finish(detail.ok === true);
    };
    const timer = window.setTimeout(() => finish(false), 8_000);
    document.addEventListener(eventName, onAck as EventListener);
    document.dispatchEvent(new CustomEvent('polaris:download-batch:v2', { detail: payload }));
  });
}
