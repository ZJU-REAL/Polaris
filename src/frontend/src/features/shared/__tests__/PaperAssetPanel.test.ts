import { describe, expect, it } from 'vitest';
import { parseStateMeta, vectorStateMeta } from '../PaperAssetPanel';

describe('PaperAssetPanel status mapping', () => {
  it('keeps MinerU and PyMuPDF fallback stages distinguishable', () => {
    expect(parseStateMeta('mineru_uploading').label).toContain('MinerU');
    expect(parseStateMeta('mineru_processing').tone).toBe('accent');
    expect(parseStateMeta('fallback_parsing').label).toContain('PyMuPDF');
    expect(parseStateMeta('failed').tone).toBe('danger');
  });

  it('reports paper and chunk vector states independently', () => {
    expect(vectorStateMeta('ready').tone).toBe('success');
    expect(vectorStateMeta('building').tone).toBe('accent');
    expect(vectorStateMeta('failed').tone).toBe('danger');
  });
});
