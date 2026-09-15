import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = join(__dirname, '..', '..', '..');
const read = (p: string) => readFileSync(join(SRC, p), 'utf8');

/**
 * 设置入口合一（#755）。
 *
 * 这几条钉的是**互相抵消**的那类错误：两个页面对跳会变成死循环，而循环在浏览器里
 * 表现为白屏或地址栏抖动，看代码时两边各自都「很合理」。
 */
describe('one settings entrance', () => {
  it('routes /admin to /settings', () => {
    const routes = read('app/routes.tsx');
    expect(routes).toContain('AdminRedirect');
    expect(routes).toContain("'/settings' + location.search");
  });

  it('does not send any settings tab back to /admin', () => {
    // 反向重定向还在的话，/settings?tab=llm → /admin?tab=llm → /settings?tab=llm … 死循环
    const settings = read('features/settings/SettingsPage.tsx');
    expect(settings).not.toContain('/admin?tab=');
    expect(settings).not.toContain('to="/admin"');
  });

  it('keeps no Manage entry in the shell', () => {
    const shell = read('app/AppShell.tsx');
    expect(shell).not.toContain('/admin');
  });

  it('renders the former admin tabs inside the settings page', () => {
    const settings = read('features/settings/SettingsPage.tsx');
    for (const tab of ['llm', 'literature', 'processing', 'experiment', 'daily', 'usage']) {
      expect(settings).toContain(`effectiveTab === '${tab}'`);
    }
  });
});
