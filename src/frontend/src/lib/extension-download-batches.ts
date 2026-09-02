import {
  api,
  type DownloadBatchCreated,
  type DownloadBatchRead,
  type PaperRead,
} from './api';
import {
  dispatchablePolarisExtensionPapers,
  dispatchPolarisExtensionBatch,
  type PolarisExtensionPaper,
} from './polaris-extension';

export type ExtensionDownloadPaper = Pick<PaperRead, 'id' | 'title' | 'doi' | 'url'>;

export interface ExtensionBatchOutcome {
  batch: DownloadBatchCreated;
  acknowledged: boolean;
  dispatchedCount: number;
}

export interface DownloadBatchCounts {
  total: number;
  active: number;
  cached: number;
  uploaded: number;
  failed: number;
}

export function extensionPapersForLibrary(
  libraryId: string,
  papers: ExtensionDownloadPaper[],
): PolarisExtensionPaper[] {
  return papers.map((paper) => ({
    libraryId,
    paperId: paper.id,
    title: paper.title,
    doi: paper.doi,
    articleUrl: paper.url,
    pdfCandidates: [],
  }));
}

export async function sendLibraryPapersToExtension(
  libraryId: string,
  papers: ExtensionDownloadPaper[],
): Promise<ExtensionBatchOutcome> {
  if (papers.length === 0) throw new Error('NO_DOWNLOAD_TARGETS');
  const extensionPapers = extensionPapersForLibrary(libraryId, papers);
  const batch = await api.createDownloadBatch(extensionPapers.map((paper) => ({
    library_id: paper.libraryId,
    paper_id: paper.paperId,
    article_url: paper.articleUrl,
    pdf_candidates: paper.pdfCandidates,
  })));
  const dispatchable = dispatchablePolarisExtensionPapers(extensionPapers, batch.items);
  const acknowledged = dispatchable.length > 0
    ? await dispatchPolarisExtensionBatch({ batchId: batch.id, papers: dispatchable })
    : false;
  return { batch, acknowledged, dispatchedCount: dispatchable.length };
}

export function countDownloadBatchItems(batch: Pick<DownloadBatchRead, 'items'>): DownloadBatchCounts {
  const counts: DownloadBatchCounts = { total: batch.items.length, active: 0, cached: 0, uploaded: 0, failed: 0 };
  for (const item of batch.items) {
    if (item.status === 'skipped') counts.cached += 1;
    else if (item.status === 'uploaded') counts.uploaded += 1;
    else if (['failed', 'blocked', 'cancelled'].includes(item.status)) counts.failed += 1;
    else counts.active += 1;
  }
  return counts;
}
