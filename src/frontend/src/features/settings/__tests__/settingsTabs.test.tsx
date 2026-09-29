import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { renderToStaticMarkup } from 'react-dom/server';
import { SettingsLayout, SettingsTabs } from '../SettingsTabs';

const read = (p: string) => readFileSync(join(__dirname, '..', p), 'utf8');

/**
 * 设置页导航：左侧分组竖排，主区窄时换成分组下拉框。
 *
 * 之前两版都在「一行排不下」上出问题：Segmented 把中文压成一字一行；两行折行的标签
 * 起点随组名宽度错开、组名和标签分不出层级。
 */
function render() {
  const client = new QueryClient();
  return renderToStaticMarkup(
    <QueryClientProvider client={client}>
      <SettingsLayout
        nav={
          <SettingsTabs
            groups={[
              { label: 'Personal', items: [{ v: 'personal', label: 'Profile' }, { v: 'prefs', label: 'Interface' }] },
              { label: 'Workspace', items: [{ v: 'llm', label: 'Models & routing' }] },
              { label: 'Empty', items: [] },
            ]}
            value="llm"
            onChange={() => {}}
          />
        }
      >
        <p>content</p>
      </SettingsLayout>
    </QueryClientProvider>,
  );
}

describe('settings navigation', () => {
  it('renders grouped vertical items with the current one marked', () => {
    const html = render();
    expect(html).toContain('settings-nav-heading');
    expect(html).toMatch(/class="settings-nav-item active"[^>]*>Models &amp; routing</);
    expect(html).toContain('aria-current="page"');
    expect(html).not.toContain('>Empty<');
  });

  it('offers the same sections as a grouped select for narrow layouts', () => {
    const html = render();
    expect(html).toContain('<optgroup label="Personal">');
    expect(html).toContain('<option value="llm" selected="">Models &amp; routing</option>');
  });

  it('keeps the page on the two-column layout', () => {
    const page = read('SettingsPage.tsx');
    expect(page).toContain('<SettingsLayout');
    expect(page).not.toMatch(/<Segmented options=\{items\}/);
  });

  it('never wraps an item and swaps to the select when the main area is narrow', () => {
    const css = readFileSync(join(__dirname, '..', '..', '..', 'styles', 'global.css'), 'utf8');
    expect(css).toMatch(/\.settings-nav-item \{[^}]*white-space: nowrap/);
    const narrow = css.slice(css.indexOf('@container mainarea (max-width: 900px)'));
    expect(narrow.slice(0, 600)).toContain('.settings-nav-select { display: block');
  });
});
