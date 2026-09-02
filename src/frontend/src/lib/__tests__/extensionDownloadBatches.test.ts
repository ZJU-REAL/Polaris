import { describe, expect, it } from 'vitest';
import {
  canSendToExtension,
  countDownloadBatchItems,
  extensionPapersForLibrary,
} from '../extension-download-batches';

describe('library extension batches', () => {
  it('maps every paper to an independent library binding', () => {
    const papers = extensionPapersForLibrary('library-a', [
      { id: 'paper-a', title: 'A', doi: '10.1000/a', url: 'https://example.test/a' },
      { id: 'paper-b', title: 'B', doi: null, url: null },
    ]);

    expect(papers.map((paper) => [paper.libraryId, paper.paperId])).toEqual([
      ['library-a', 'paper-a'],
      ['library-a', 'paper-b'],
    ]);
  });

  it('separates cached, uploaded, active, and failed item states', () => {
    const item = (status: string) => ({ status });
    const counts = countDownloadBatchItems({
      items: [item('queued'), item('downloading'), item('skipped'), item('uploaded'), item('failed')],
    } as never);

    expect(counts).toEqual({ total: 5, active: 2, cached: 1, uploaded: 1, failed: 1 });
  });
});

describe('extension sendability gate', () => {
  it('mirrors the backend downloadable status set', () => {
    // 后端 DOWNLOADABLE_LIBRARY_STATUSES 之外的一律 404。UI 若不挡住，
    // 用户对候选论文点"推送扩展"只会拿到一个看不懂的错误。
    expect(canSendToExtension('scored')).toBe(true);
    expect(canSendToExtension('fetched')).toBe(true);
    expect(canSendToExtension('compiled')).toBe(true);
    expect(canSendToExtension('included')).toBe(true);
    expect(canSendToExtension('candidate')).toBe(false);
    expect(canSendToExtension('excluded')).toBe(false);
    expect(canSendToExtension(null)).toBe(false);
    expect(canSendToExtension(undefined)).toBe(false);
  });
});
