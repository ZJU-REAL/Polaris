import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const read = (p: string) => readFileSync(join(__dirname, '..', p), 'utf8');

/**
 * 设置页的标签导航。十七八个标签塞进一条不折行的 Segmented 时，每个按钮被压到一字宽，
 * 中文标签竖着一字一行。钉住：导航用可折行的分组，标签本身不断行。
 */
describe('settings tab navigation', () => {
  it('uses the wrapping grouped tabs, not a single segmented bar', () => {
    const page = read('SettingsPage.tsx');
    expect(page).toContain('<SettingsTabs');
    expect(page).not.toMatch(/<Segmented options=\{items\}/);
  });

  it('wraps rows and never breaks a label', () => {
    const tabs = read('SettingsTabs.tsx');
    expect(tabs).toContain("flexWrap: 'wrap'");
    expect(tabs).toContain("whiteSpace: 'nowrap'");
  });
});
