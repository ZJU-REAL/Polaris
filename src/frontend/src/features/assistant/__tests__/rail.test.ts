import { describe, expect, it } from 'vitest';
import { groupByRecency, type ConversationRow } from '../ConversationRail';

/* 会话分组：几十场对话平铺成一列时「上周那次」根本找不到，而时间正是人回忆对话
   时用的线索。这里钉住边界——分组算错会让对话看起来「不见了」。 */

const now = new Date(2026, 7, 5, 14, 30); // 本地 2026-08-05 14:30
const row = (id: string, when: Date | null): ConversationRow => ({
  id,
  title: id,
  last_message_at: when ? when.toISOString() : null,
});

describe('按最近活跃分组', () => {
  it('今天/昨天/7 天内/更早各就各位', () => {
    const groups = groupByRecency(
      [
        row('今天早些', new Date(2026, 7, 5, 1, 0)),
        row('昨天', new Date(2026, 7, 4, 23, 59)),
        row('三天前', new Date(2026, 7, 2, 12, 0)),
        row('上个月', new Date(2026, 6, 1, 12, 0)),
      ],
      now,
    );
    expect(groups.map((g) => g.rows.map((r) => r.id))).toEqual([
      ['今天早些'],
      ['昨天'],
      ['三天前'],
      ['上个月'],
    ]);
  });

  it('空分组不出现——不摆一个空标题占地方', () => {
    const groups = groupByRecency([row('只有今天', new Date(2026, 7, 5, 9, 0))], now);
    expect(groups).toHaveLength(1);
  });

  it('刚建、还没说过话的归到今天：它就在眼前，扔进「更早」很怪', () => {
    const groups = groupByRecency([row('新建的', null)], now);
    expect(groups[0]?.rows.map((r) => r.id)).toEqual(['新建的']);
  });

  it('今天零点整算今天，差一毫秒算昨天', () => {
    const midnight = new Date(2026, 7, 5, 0, 0, 0, 0);
    const justBefore = new Date(midnight.getTime() - 1);
    const groups = groupByRecency([row('零点', midnight), row('零点前', justBefore)], now);
    expect(groups[0]?.rows.map((r) => r.id)).toEqual(['零点']);
    expect(groups[1]?.rows.map((r) => r.id)).toEqual(['零点前']);
  });
});
