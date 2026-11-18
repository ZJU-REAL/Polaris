import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import type { EngineBootstrapStatus, EngineFailure, HostEvent } from '../../lib/host';
import { setLang } from '../../lib/i18n';
import { EngineBootstrapPage } from './EngineBootstrapPage';
import { EngineUnavailablePage, unavailableCopy } from './EngineUnavailablePage';
import { EngineRestartingOverlay, engineWatchAction } from './engineWatch';
import { NetworkSettingsForm, mirrorOptions, networkErrorText } from './NetworkSettingsForm';
import { SupportActions, runCopyDiagnostics, supportNoteText } from './SupportActions';

/* 安装失败 / 引擎不可用时页面说的话。以前一律是「请检查网络连接」，没有原因、
   没有日志、没有任何可以发给我们的东西。 */

const failure = (code: EngineFailure['code'], lines: string[] = []): EngineFailure => ({
  code,
  message: 'engine-bootstrap: install step failed after 4 attempt(s): exited with code=2',
  lines,
  at: 0,
});

beforeEach(() => setLang('zh'));
afterEach(() => setLang('zh'));

describe('unavailableCopy', () => {
  it('names the actual cause instead of always blaming the network', () => {
    expect(unavailableCopy({ setupFailed: true, failure: failure('disk-space') }).title).toBe('磁盘空间不足');
    expect(unavailableCopy({ setupFailed: true, failure: failure('download') }).title).toBe('下载没有完成');
    expect(unavailableCopy({ failure: failure('engine-crashed') }).title).toBe('本机引擎意外停止');
    expect(unavailableCopy({ failure: failure('engine-start') }).title).toBe('本机引擎没有启动');
    expect(unavailableCopy({ setupFailed: true, failure: failure('install') }).title).toBe('安装没有完成');
  });

  it('offers network settings only where the network is the likely cause', () => {
    expect(unavailableCopy({ setupFailed: true, failure: failure('download') }).network).toBe(true);
    expect(unavailableCopy({ setupFailed: true }).network).toBe(true);
    expect(unavailableCopy({ setupFailed: true, failure: failure('disk-space') }).network).toBe(false);
    expect(unavailableCopy({ failure: failure('engine-crashed') }).network).toBe(false);
    expect(unavailableCopy({}).network).toBe(false);
  });

  it('a port problem wins over the generic failure, in both languages', () => {
    expect(unavailableCopy({ problem: 'port-in-use', failure: failure('engine-start') }).title).toContain('18080');
    setLang('en');
    expect(unavailableCopy({ problem: 'stale-engine', failure: failure('engine-start') }).title).toBe(
      'Another copy of Polaris is still running',
    );
    expect(unavailableCopy({ setupFailed: true, failure: failure('download') }).detail).toContain('China mirrors');
  });

  it('never tells the user to "check your network" for a full disk or a crash', () => {
    for (const code of ['disk-space', 'engine-crashed', 'engine-start', 'install'] as const) {
      const copy = unavailableCopy({ setupFailed: true, failure: failure(code) });
      expect(copy.detail).not.toMatch(/网络/);
    }
  });
});

describe('engine unavailable page with a failure', () => {
  const lines = ['error: Failed to fetch: https://pypi.org/simple/fastapi/', '  Caused by: operation timed out'];

  it('shows the last error lines and both support actions', () => {
    const html = renderToStaticMarkup(
      <EngineUnavailablePage setupFailed failure={failure('download', lines)} canRelaunch showSupport />,
    );
    expect(html).toContain('engine-failure-lines');
    expect(html).toContain('Failed to fetch: https://pypi.org/simple/fastapi/');
    expect(html).toContain('operation timed out');
    expect(html).toContain('打开日志文件夹');
    expect(html).toContain('复制诊断信息');
    expect(html).toContain('网络设置');
    expect(html).not.toContain('请检查网络连接，然后重新打开');
  });

  it('keeps the page minimal when there is nothing to show', () => {
    const html = renderToStaticMarkup(<EngineUnavailablePage failure={null} canRelaunch={false} showSupport={false} />);
    expect(html).not.toContain('engine-failure-lines');
    expect(html).not.toContain('打开日志文件夹');
    expect(html).toContain('本机引擎没有运行');
  });

  it('does not offer network settings for a crash', () => {
    const html = renderToStaticMarkup(
      <EngineUnavailablePage failure={failure('engine-crashed', ['Segmentation fault'])} canRelaunch showSupport />,
    );
    expect(html).toContain('本机引擎意外停止');
    expect(html).toContain('Segmentation fault');
    expect(html).not.toContain('网络设置');
  });
});

describe('bootstrap page: restart and failure states', () => {
  const render = (status: EngineBootstrapStatus): string =>
    renderToStaticMarkup(<EngineBootstrapPage initialStatus={status} onProceed={() => undefined} />);

  it('a mid-session restart is not presented as first-run setup', () => {
    const html = render({ phase: 'engine', done: false, restarting: true });
    expect(html).toContain('engine-restarting');
    expect(html).toContain('本机引擎已停止');
    expect(html).toContain('正在重新启动');
    expect(html).not.toContain('下载 Python');
    expect(html).not.toContain('首次启动需要几分钟');
  });

  it('the normal engine-start phase still shows the setup steps', () => {
    const html = render({ phase: 'engine', done: false });
    expect(html).not.toContain('engine-restarting');
    expect(html).toContain('启动引擎');
  });
});

describe('engineWatchAction', () => {
  const state = (s: 'running' | 'restarting' | 'failed'): HostEvent => ({ type: 'engine.state', state: s });

  it('covers the page while the engine restarts and uncovers it when it is back', () => {
    expect(engineWatchAction(state('restarting'), false)).toBe('show');
    expect(engineWatchAction(state('restarting'), true)).toBe('ignore');
    expect(engineWatchAction(state('running'), true)).toBe('hide');
    expect(engineWatchAction(state('running'), false)).toBe('ignore');
  });

  it('reloads into the failure page when the engine could not be brought back', () => {
    expect(engineWatchAction(state('failed'), true)).toBe('reload');
    expect(engineWatchAction(state('failed'), false)).toBe('reload');
  });

  it('ignores unrelated host events', () => {
    expect(engineWatchAction({ type: 'job.done', jobId: 'j', result: null }, false)).toBe('ignore');
  });

  it('the overlay says what is happening in plain words', () => {
    const html = renderToStaticMarkup(<EngineRestartingOverlay />);
    expect(html).toContain('本机引擎已停止');
    expect(html).toContain('正在重新启动');
  });
});

describe('support actions', () => {
  it('copies the diagnostics text and reports the outcome honestly', async () => {
    const written: string[] = [];
    const ok = await runCopyDiagnostics(
      async () => ({ text: 'Polaris 0.6.0\nEngine: 0.6.0' }),
      async (text) => {
        written.push(text);
        return true;
      },
    );
    expect(ok).toBe('copied');
    expect(written).toEqual(['Polaris 0.6.0\nEngine: 0.6.0']);

    // an older shell has no diagnostics method; a blurred window can refuse the clipboard
    expect(await runCopyDiagnostics(async () => null, async () => true)).toBe('copy-failed');
    expect(await runCopyDiagnostics(async () => ({ text: 'x' }), async () => false)).toBe('copy-failed');
    expect(
      await runCopyDiagnostics(async () => {
        throw new Error('bridge down');
      }, async () => true),
    ).toBe('copy-failed');
  });

  it('renders both buttons and the feedback line', () => {
    const html = renderToStaticMarkup(<SupportActions initialNote="copied" />);
    expect(html).toContain('打开日志文件夹');
    expect(html).toContain('复制诊断信息');
    expect(html).toContain('已复制');
    setLang('en');
    expect(renderToStaticMarkup(<SupportActions />)).toContain('Open logs folder');
    expect(supportNoteText('open-failed')).toBe('Couldn’t open the logs folder');
  });
});

describe('network settings form', () => {
  it('renders the stored proxy and mirror choice', () => {
    const html = renderToStaticMarkup(
      <NetworkSettingsForm initial={{ proxy: 'http://127.0.0.1:7890', mirror: 'on' }} />,
    );
    expect(html).toContain('value="http://127.0.0.1:7890"');
    expect(html).toContain('国内镜像');
    expect(html).toMatch(/<option value="on" selected="">优先使用<\/option>/);
    // nothing changed yet: saving is disabled
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>保存<\/button>/);
  });

  it('renders nothing when the host has no network settings (older shell)', () => {
    expect(renderToStaticMarkup(<NetworkSettingsForm />)).toBe('');
  });

  it('turns the host validation error into a sentence with an example', () => {
    expect(networkErrorText(new Error('ERR_INVALID_PARAMS: proxy must look like http://127.0.0.1:7890'))).toContain(
      'http://127.0.0.1:7890',
    );
    expect(networkErrorText(new Error('boom'))).toBe('无法保存');
    expect(mirrorOptions().map((o) => o.v)).toEqual(['auto', 'on', 'off']);
  });
});
