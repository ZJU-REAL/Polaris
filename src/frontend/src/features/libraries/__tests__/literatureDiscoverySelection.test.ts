import { describe, expect, it } from 'vitest';
import type { LiteratureOaCache, LiteratureSearchHit } from '../../../lib/api';
import { eligibleExtensionHits } from '../LiteratureDiscoveryPage';

function hit(id: string, status: LiteratureSearchHit['status'], paperId: string | null): LiteratureSearchHit {
  return { id, status, paper_id: paperId } as LiteratureSearchHit;
}

describe('eligibleExtensionHits', () => {
  it('requires an explicit selection and a promoted library paper binding', () => {
    const candidate = hit('candidate', 'candidate', null);
    const promoted = hit('promoted', 'promoted', 'paper-1');
    expect(eligibleExtensionHits(
      [candidate, promoted],
      new Set(['candidate', 'promoted']),
      new Map(),
    )).toEqual([promoted]);
  });

  it('skips a promoted paper whose OA PDF is already cached', () => {
    const promoted = hit('promoted', 'promoted', 'paper-1');
    const cache = { hit_id: 'promoted', status: 'ready' } as LiteratureOaCache;
    expect(eligibleExtensionHits(
      [promoted],
      new Set(['promoted']),
      new Map([['promoted', cache]]),
    )).toEqual([]);
  });
});
