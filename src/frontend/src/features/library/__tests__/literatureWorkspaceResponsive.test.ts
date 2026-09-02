import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const css = readFileSync(
  fileURLToPath(new URL('../../../styles/global.css', import.meta.url)),
  'utf-8',
);
const personalLibrary = readFileSync(
  fileURLToPath(new URL('../LibraryPage.tsx', import.meta.url)),
  'utf-8',
);
const projectLibrary = readFileSync(
  fileURLToPath(new URL('../../wiki/WikiPage.tsx', import.meta.url)),
  'utf-8',
);
const publicLibrary = readFileSync(
  fileURLToPath(new URL('../../libraries/LibraryBrowse.tsx', import.meta.url)),
  'utf-8',
);

describe('literature workspace responsive layout', () => {
  it('marks each literature workspace without changing shared split behavior', () => {
    for (const source of [personalLibrary, projectLibrary, publicLibrary]) {
      expect(source).toContain('literature-workspace-tabs');
      expect(source).toContain('literature-workspace-card');
    }
  });

  it('uses the main content container and preserves complete tab labels', () => {
    expect(css).toContain('@container mainarea (max-width: 1180px)');
    expect(css).toContain('.literature-workspace-card .split { flex-direction: column; }');
    expect(css).toMatch(/\.literature-workspace-tabs \.segmented > button[\s\S]*white-space: nowrap/);
  });
});
