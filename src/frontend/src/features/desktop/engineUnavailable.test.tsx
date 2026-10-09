import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { EngineUnavailablePage, runRelaunch } from './EngineUnavailablePage';

/* #850：没有桌面宿主时「重新打开」什么也做不了，别给这个按钮；「再检查一次」永远能点。 */

// 只看卡片里的按钮（顶上还有语言切换）
const buttons = (html: string) => html.slice(html.indexOf('auth-card')).match(/<button[^>]*>/g) ?? [];

describe('engine unavailable page', () => {
  it('without a desktop host shows only "Check again", enabled', () => {
    const html = renderToStaticMarkup(<EngineUnavailablePage canRelaunch={false} />);
    expect(buttons(html)).toHaveLength(1);
    expect(html).toContain('再检查一次');
    expect(buttons(html)[0]).not.toContain('disabled');
  });

  it('while restarting, only the restart button is disabled', () => {
    const html = renderToStaticMarkup(<EngineUnavailablePage canRelaunch initialRestarting />);
    const [check, restart] = buttons(html);
    expect(check).not.toContain('disabled');
    expect(restart).toContain('disabled');
    expect(html).toContain('正在重新打开');
  });

  it('resets after the relaunch call returns or fails', async () => {
    const states: boolean[] = [];
    await runRelaunch(async () => undefined, (v) => states.push(v));
    expect(states).toEqual([true, false]);
    const failed: boolean[] = [];
    await runRelaunch(async () => {
      throw new Error('no host');
    }, (v) => failed.push(v));
    expect(failed).toEqual([true, false]);
  });
});
