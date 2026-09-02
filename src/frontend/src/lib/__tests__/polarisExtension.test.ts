import { describe, expect, it } from 'vitest';
import {
  createPolarisExtensionPayload,
  dispatchablePolarisExtensionPapers,
} from '../polaris-extension';

describe('Polaris extension batch payload', () => {
  it('keeps every paper independently bound inside one batch', () => {
    let counter = 0;
    const payload = createPolarisExtensionPayload({
      batchId: '44444444-4444-4444-8444-444444444444',
      papers: Array.from({ length: 10 }, (_, index) => ({
        libraryId: '11111111-1111-4111-8111-111111111111',
        paperId: `22222222-2222-4222-8222-${String(index).padStart(12, '0')}`,
        title: `Paper ${index + 1}`,
        doi: `10.1000/${index + 1}`,
      })),
    }, 'https://polaris.example', new Date('2026-08-15T00:00:00Z'), () => `uuid-${counter += 1}`);

    expect(payload.version).toBe(2);
    expect(payload.backend_batch_id).toBe('44444444-4444-4444-8444-444444444444');
    expect(payload.papers).toHaveLength(10);
    expect(new Set(payload.papers.map((paper) => paper.nonce)).size).toBe(10);
    expect(payload.papers.every((paper) => paper.library_id && paper.paper_id)).toBe(true);
  });

  it('dispatches only queued bindings without shifting another paper', () => {
    const papers = [
      { libraryId: 'library-a', paperId: 'paper-a', title: 'Cached paper' },
      { libraryId: 'library-a', paperId: 'paper-b', title: 'Queued paper' },
    ];
    const result = dispatchablePolarisExtensionPapers(papers, [
      { library_id: 'library-a', paper_id: 'paper-b', status: 'queued' },
      { library_id: 'library-a', paper_id: 'paper-a', status: 'skipped' },
    ]);

    expect(result).toEqual([papers[1]]);
  });
});
