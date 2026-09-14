import { describe, expect, it } from 'vitest';
import { dailySubscriptionPayload } from '../SettingsPage';
import type { DailySubscription } from '../../../lib/api';

const arxiv: DailySubscription[] = [
  { source: 'arxiv', terms: ['cs.AI', 'cs.CL'], supports_daily: true },
];
const others: DailySubscription[] = [
  { source: 'pubmed', terms: ['neuroscience'], supports_daily: true },
];

describe('daily subscription payload', () => {
  it('carries the arxiv subscription through when saving other sources', () => {
    // PUT 是整份替换：漏掉 arXiv 那条等于在保存「其他来源」时把分类订阅清空，
    // 当天就表现为池子空了，而操作的人只是加了个检索词
    const payload = dailySubscriptionPayload(arxiv, others);
    expect(payload).toEqual([
      { source: 'arxiv', terms: ['cs.AI', 'cs.CL'] },
      { source: 'pubmed', terms: ['neuroscience'] },
    ]);
  });

  it('drops the supports_daily flag, which is read-only state from the server', () => {
    const payload = dailySubscriptionPayload([], others);
    expect(payload[0]).not.toHaveProperty('supports_daily');
  });

  it('removing every other source still keeps arxiv', () => {
    expect(dailySubscriptionPayload(arxiv, [])).toEqual([
      { source: 'arxiv', terms: ['cs.AI', 'cs.CL'] },
    ]);
  });
});
