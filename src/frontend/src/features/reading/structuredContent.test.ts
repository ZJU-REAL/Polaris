import { describe, expect, it } from 'vitest';
import { resolveStructuredResourceUrls } from './structuredContent';

describe('resolveStructuredResourceUrls', () => {
  it('preserves web same-origin signed URLs', () => {
    const source = '![figure](/api/structured-content-assets/token-1)';
    expect(resolveStructuredResourceUrls(source)).toBe(source);
  });

  it('does not rewrite unrelated paths', () => {
    expect(resolveStructuredResourceUrls('[paper](/papers/1)')).toBe('[paper](/papers/1)');
  });
});
