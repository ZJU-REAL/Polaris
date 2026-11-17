import { afterEach, describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { setLang } from '../../../lib/i18n';
import type { IdeaStatus } from '../../../lib/api';
import { IdeaActionCard } from '../IdeaActionCard';

/**
 * 想法页的「下一步」卡：选中直接生效（没有审批），选中之后的主操作是开始实验。
 *
 * 以前选中要先建一条审批、再由同一个人去批；批完这张卡只剩「已晋级」和两个禁用
 * 按钮，开始实验要回到评审排行榜才找得到。
 */
function render(status: IdeaStatus, busy = false) {
  const noop = () => {};
  return renderToStaticMarkup(
    <IdeaActionCard
      status={status}
      busy={busy}
      onPick={noop}
      onUnpick={noop}
      onReject={noop}
      onRestore={noop}
      onStartExperiment={noop}
    />,
  );
}

/** 按钮文字（按出现顺序）和哪一个是主按钮。 */
function buttons(html: string) {
  const found = [...html.matchAll(/<button class="([^"]*)"([^>]*)>(.*?)<\/button>/g)];
  return found.map((m) => ({
    label: (m[3] ?? '').replace(/<svg.*?<\/svg>/g, '').trim(),
    primary: (m[1] ?? '').includes('btn-primary'),
    disabled: (m[2] ?? '').includes('disabled'),
  }));
}

afterEach(() => setLang('zh'));

describe('idea next-step card', () => {
  it.each(['candidate', 'under_review'] as const)('offers Pick as the primary action for a %s idea', (status) => {
    setLang('en');
    expect(buttons(render(status))).toEqual([
      { label: 'Pick', primary: true, disabled: false },
      { label: 'Reject', primary: false, disabled: false },
    ]);
  });

  it('leads a picked idea to an experiment and lets the user unpick it', () => {
    setLang('en');
    const html = render('promoted');
    expect(buttons(html)).toEqual([
      { label: 'Start experiment', primary: true, disabled: false },
      { label: 'Unpick', primary: false, disabled: false },
    ]);
    expect(html).toContain('Picked.');
  });

  it('lets a rejected idea come back', () => {
    setLang('en');
    expect(buttons(render('rejected'))).toEqual([{ label: 'Restore as candidate', primary: false, disabled: false }]);
  });

  it('has exactly one primary button unless the idea is rejected', () => {
    for (const status of ['candidate', 'under_review', 'promoted'] as const) {
      expect(buttons(render(status)).filter((b) => b.primary)).toHaveLength(1);
    }
    expect(buttons(render('rejected')).filter((b) => b.primary)).toHaveLength(0);
  });

  it('disables every button while a change is in flight', () => {
    for (const status of ['candidate', 'promoted', 'rejected'] as const) {
      expect(buttons(render(status, true)).every((b) => b.disabled)).toBe(true);
    }
  });

  it('says 选中, never 晋级 or approval, in Chinese', () => {
    setLang('zh');
    const all = (['candidate', 'promoted', 'rejected'] as const).map((s) => render(s)).join('');
    expect(all).toContain('选中');
    expect(all).toContain('开始实验');
    expect(all).not.toMatch(/晋级|审批|Promot/);
  });
});
