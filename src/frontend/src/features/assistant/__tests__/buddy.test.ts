import { describe, expect, it } from 'vitest';
import { pageContextFrom } from '../buddyContext';
import { alreadyNudgedToday, markNudged, todayKey } from '../nudge';
import { phaseOf } from '../TurnStatus';
import type { AssistantBlock } from '../../../lib/assistantStream';

describe('页面感知', () => {
  it('从路由认出用户在看什么', () => {
    const id = '11111111-2222-3333-4444-555555555555';
    expect(pageContextFrom(`/papers/${id}/read`)).toMatchObject({ kind: 'paper', id });
    expect(pageContextFrom(`/ideas/${id}`)).toMatchObject({ kind: 'idea', id });
    expect(pageContextFrom('/daily')).toMatchObject({ kind: 'daily' });
  });

  it('认不出的路由返回 null——Buddy 照常工作，只是不带上下文', () => {
    expect(pageContextFrom('/settings')).toBeNull();
    expect(pageContextFrom('/papers/not-a-uuid/read')).toBeNull();
    expect(pageContextFrom('')).toBeNull();
  });
});

describe('主动提示的记账', () => {
  it('按本地日期算「今天」——用户感知的今天是他自己时区的今天', () => {
    // 本地时间 2026-08-05 00:30，UTC 还是 08-04（东八区）；键要跟着本地走
    const key = todayKey(new Date(2026, 7, 5, 0, 30));
    expect(key).toBe('2026-8-5');
  });

  it('提过之后当天不再提', () => {
    const store: Record<string, string> = {};
    const now = new Date(2026, 7, 5, 9, 0);
    expect(alreadyNudgedToday(now, () => store.k ?? null)).toBe(false);
    markNudged(now, (v) => {
      store.k = v;
    });
    expect(alreadyNudgedToday(now, () => store.k ?? null)).toBe(true);
    // 跨到第二天又该提了
    expect(alreadyNudgedToday(new Date(2026, 7, 6, 9, 0), () => store.k ?? null)).toBe(false);
  });

  it('读写失败当作「没提过」：重复提一次，好过因为存不下就永远不提', () => {
    const boom = () => {
      throw new Error('隐私模式');
    };
    expect(alreadyNudgedToday(new Date(), boom)).toBe(false);
    expect(() =>
      markNudged(new Date(), () => {
        throw new Error('配额满');
      }),
    ).not.toThrow();
  });
});

describe('本轮状态', () => {
  const tool = (state: 'running' | 'ok'): AssistantBlock => ({
    kind: 'tool',
    id: 't',
    name: 'search_papers',
    state,
  });

  it('状态只从收到的块推，不另立状态机', () => {
    expect(phaseOf([], false)).toBe('idle');
    expect(phaseOf([], true)).toBe('thinking');
    expect(phaseOf([tool('running')], true)).toBe('tool');
    expect(phaseOf([{ kind: 'text', text: '答' }], true)).toBe('writing');
  });

  it('工具跑完就不再说「正在查」——#249 那个毛病正是两套状态各说各话', () => {
    expect(phaseOf([tool('ok')], true)).not.toBe('tool');
  });

  it('本轮结束后一律 idle，不管最后一个块是什么', () => {
    expect(phaseOf([tool('running')], false)).toBe('idle');
  });
});
