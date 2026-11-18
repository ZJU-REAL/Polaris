import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { extensionDeliveryAvailable } from '../../lib/polaris-extension';

/* 桌面应用里，下载批次到不了浏览器扩展：扩展只听「Polaris 网页」上的页面事件，
   而桌面界面在 Electron 窗口里（app://polaris），扩展也没有主动领取的代码。
   以前这些按钮照常显示，发完还提示「浏览器扩展连接后会领取」——不会。
   在扩展补上领取之前，桌面应用里不摆这些入口。 */

afterEach(() => vi.unstubAllGlobals());

describe('extensionDeliveryAvailable', () => {
  it('is false inside the desktop app', () => {
    vi.stubGlobal('window', { __POLARIS__: { platform: 'darwin', appVersion: '0.6.0' } });
    expect(extensionDeliveryAvailable()).toBe(false);
  });

  it('stays true in a browser page, where the extension can attach', () => {
    vi.stubGlobal('window', {});
    expect(extensionDeliveryAvailable()).toBe(true);
  });
});

describe('every "send to browser extension" entry point is gated', () => {
  const SRC = join(__dirname, '..', '..');
  const read = (path: string): string => readFileSync(join(SRC, path), 'utf8');

  /** 每处按钮文案往前一小段之内，必须出现那道闸。 */
  const gatedOccurrences = (source: string, label: string): boolean[] => {
    const out: boolean[] = [];
    let at = source.indexOf(label);
    while (at >= 0) {
      out.push(source.slice(Math.max(0, at - 900), at).includes('extensionDeliveryAvailable()'));
      at = source.indexOf(label, at + label.length);
    }
    return out;
  };

  it('library search results', () => {
    const hits = gatedOccurrences(read('features/libraries/LiteratureDiscoveryPage.tsx'), 'Send to browser extension ·');
    expect(hits.length).toBeGreaterThan(0);
    expect(hits.every(Boolean)).toBe(true);
  });

  it('paper list: per-paper menu item, batch button and the history entry', () => {
    const source = read('features/wiki/PapersTab.tsx');
    const perPaper = gatedOccurrences(source, "tr('用浏览器扩展下载 PDF', 'Get the PDF via the browser extension')");
    const batch = gatedOccurrences(source, "tr('用浏览器扩展下载 PDF', 'Get PDFs via the extension')");
    expect(perPaper).toEqual([true]);
    expect(batch).toEqual([true]);
    // 下载记录在文献库页头的「更多」菜单里
    const page = read('features/wiki/WikiPage.tsx');
    expect(gatedOccurrences(page, "tr('浏览器扩展下载记录', 'Browser extension downloads')")).toEqual([true]);
  });

  it('the settings tab that only exists to connect the extension', () => {
    const source = read('features/settings/SettingsPage.tsx');
    expect(source).toMatch(/extensionAvailable \? \[\{ v: 'extension' as Tab/);
    expect(source).toContain("(tab === 'extension' && !extensionAvailable)");
  });
});
