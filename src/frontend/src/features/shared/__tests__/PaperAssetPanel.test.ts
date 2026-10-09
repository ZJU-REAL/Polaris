import { describe, expect, it } from 'vitest';
import { parseStateMeta, vectorStateMeta } from '../PaperAssetPanel';

describe('PaperAssetPanel status mapping', () => {
  it('shows a plain label and keeps MinerU / PyMuPDF stages in the tooltip detail', () => {
    expect(parseStateMeta('mineru_uploading').label).not.toContain('MinerU');
    expect(parseStateMeta('mineru_uploading').detail).toContain('MinerU');
    expect(parseStateMeta('mineru_processing').tone).toBe('accent');
    expect(parseStateMeta('fallback_parsing').label).not.toContain('PyMuPDF');
    expect(parseStateMeta('fallback_parsing').detail).toContain('PyMuPDF');
    expect(parseStateMeta('failed').tone).toBe('danger');
  });

  it('reports paper and chunk vector states independently', () => {
    expect(vectorStateMeta('ready').tone).toBe('success');
    expect(vectorStateMeta('building').tone).toBe('accent');
    expect(vectorStateMeta('failed').tone).toBe('danger');
  });
});
