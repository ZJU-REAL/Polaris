import { describe, expect, it } from 'vitest';
import { findAllNormalizedTextMatches } from '../evidenceText';

describe('evidence text matching', () => {
  it('matches a sentence across text parts and punctuation differences', () => {
    const matches = findAllNormalizedTextMatches(
      ['The proposed ', 'SAM-3 model', ' improves accuracy by 12.5%.'],
      'The proposed SAM 3 model improves accuracy by 12.5%.',
    );

    expect(matches).toEqual([
      { startPart: 0, startOffset: 0, endPart: 2, endOffset: 26 },
    ]);
  });

  it('returns every duplicate so callers cannot silently choose the first', () => {
    expect(findAllNormalizedTextMatches(
      ['Repeated evidence sentence.', 'Repeated evidence sentence.'],
      'Repeated evidence sentence.',
    )).toHaveLength(2);
  });
});
