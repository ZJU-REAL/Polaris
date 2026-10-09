import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { EngineBootstrapStatus } from '../../lib/host';
import { setLang } from '../../lib/i18n';
import { EngineBootstrapPage } from './EngineBootstrapPage';

/* 首启等待页的进度细节：当前步骤下显示已下载/已用时长，详情框收着，
   长时间没动静才说一句「仍在进行」。老宿主不给这些字段，页面与从前一样。 */

const NOW = Date.UTC(2026, 9, 9, 12, 0, 0);
const MB = 1024 * 1024;

function render(status: EngineBootstrapStatus, opts: { showLog?: boolean } = {}): string {
  return renderToStaticMarkup(
    <EngineBootstrapPage initialStatus={status} onProceed={() => undefined} initialShowLog={opts.showLog} />,
  );
}

const installing: EngineBootstrapStatus = {
  phase: 'install',
  done: false,
  phaseStartedAt: NOW - 80_000,
  lastOutputAt: NOW - 2000,
  downloadedBytes: 236 * MB,
  log: ['Resolved 142 packages in 1.8s', 'Prepared 140 packages in 1m 12s'],
};

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  setLang('zh');
});

afterEach(() => {
  vi.useRealTimers();
  setLang('zh');
});

describe('engine bootstrap page progress detail', () => {
  it('shows downloaded size and elapsed time under the active step', () => {
    const html = render(installing);
    expect(html).toContain('已下载 236 MB · 1 分 20 秒');
    // 细节紧跟在「安装组件」之后，不在其他步骤下
    const detailAt = html.indexOf('bootstrap-step-detail');
    expect(html.lastIndexOf('安装组件', detailAt)).toBeGreaterThan(html.indexOf('创建运行环境'));
    expect(html.indexOf('启动引擎')).toBeGreaterThan(detailAt);
    expect(html).not.toContain('仍在进行');
  });

  it('uses the English formats when the language is English', () => {
    setLang('en');
    const html = render(installing);
    expect(html).toContain('236 MB downloaded · 1:20');
    expect(html).toContain('Show details');
  });

  it('phases without byte sampling show only the elapsed time', () => {
    const html = render({ phase: 'venv', done: false, phaseStartedAt: NOW - 5000, log: [] });
    expect(html).toContain('5 秒');
    expect(html).not.toContain('已下载');
    expect(html).not.toContain('显示详情');
  });

  it('keeps the log collapsed behind a toggle', () => {
    const collapsed = render(installing);
    expect(collapsed).toContain('显示详情');
    expect(collapsed).not.toContain('bootstrap-log');
    expect(collapsed).not.toContain('Prepared 140 packages');

    const open = render(installing, { showLog: true });
    expect(open).toContain('隐藏详情');
    expect(open).toContain('bootstrap-log');
    expect(open).toContain('Resolved 142 packages in 1.8s');
    expect(open).toContain('Prepared 140 packages in 1m 12s');
  });

  it('shows the still-working hint only after 90 s without output or growth', () => {
    const quiet: EngineBootstrapStatus = {
      phase: 'python',
      done: false,
      phaseStartedAt: NOW - 120_000,
      lastOutputAt: NOW - 95_000,
      log: ['Searching for Python 3.12'],
    };
    expect(render(quiet)).toContain('仍在进行，网络较慢时需要更久');
    expect(render({ ...quiet, lastOutputAt: NOW - 60_000 })).not.toContain('仍在进行');

    vi.setSystemTime(NOW - 30_000); // 同一状态，30 秒前看还没到阈值
    expect(render(quiet)).not.toContain('仍在进行');
  });

  it('old hosts without the extra fields render the plain step list', () => {
    const html = render({ phase: 'install', done: false });
    expect(html).toContain('安装组件');
    expect(html).not.toContain('bootstrap-step-detail');
    expect(html).not.toContain('显示详情');
  });
});
