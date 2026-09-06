import { describe, expect, it } from 'vitest';
import type { HypothesisNodeRead } from '../../../lib/api';
import {
  buildReport,
  flattenTree,
  MIN_VIABLE_SCORE,
  parseGrounding,
  parseNovelty,
  pruneReasonKind,
  stanceGroups,
  supportRatio,
} from '../discoveryEvidence';

/* discovery 证据卡纯函数（#654）：支持度是要给用户看的百分比，数字必须对——
   全 speculation、空 grounding、混合立场、脏数据降级都钉死在这里。 */

function entry(stance: string, paperIds: string[] = [], subclaim = 'sc'): unknown {
  return { subclaim, stance, paper_ids: paperIds, snippets: [] };
}

function node(over: Partial<HypothesisNodeRead>): HypothesisNodeRead {
  return {
    id: 'n1',
    run_id: 'r1',
    parent_id: null,
    kind: 'hypothesis',
    statement: 's',
    grounding: null,
    novelty_report: null,
    feasibility: null,
    score: null,
    status: 'open',
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z',
    ...over,
  };
}

describe('supportRatio', () => {
  it('空 grounding 返回 null（没数据 ≠ 支持度 0）', () => {
    expect(supportRatio([])).toBeNull();
    expect(supportRatio(parseGrounding(null))).toBeNull();
    expect(supportRatio(parseGrounding([]))).toBeNull();
  });

  it('全 speculation 支持度为 0', () => {
    const entries = parseGrounding([entry('speculation'), entry('speculation')]);
    expect(supportRatio(entries)).toBe(0);
  });

  it('混合立场按「非 speculation 且有引用」的占比计算', () => {
    const entries = parseGrounding([
      entry('support', ['p1']),
      entry('refute', ['p2']),
      entry('speculation'),
      entry('support', ['p1', 'p3']),
    ]);
    expect(supportRatio(entries)).toBe(3 / 4);
  });

  it('有立场但没引用的条目降级为 speculation，不计入支持度', () => {
    const entries = parseGrounding([entry('support', []), entry('refute', ['p1'])]);
    expect(entries[0]!.stance).toBe('speculation');
    expect(supportRatio(entries)).toBe(1 / 2);
  });
});

describe('parseGrounding', () => {
  it('脏数据（非对象/空子命题/未知立场）降级不崩', () => {
    const entries = parseGrounding([
      null,
      42,
      { stance: 'support', paper_ids: ['p1'] }, // 缺 subclaim → 丢弃
      { subclaim: 'ok', stance: 'banana', paper_ids: ['p1'] }, // 未知立场 → speculation
    ]);
    expect(entries).toHaveLength(1);
    expect(entries[0]!).toMatchObject({ subclaim: 'ok', stance: 'speculation', paperIds: [] });
  });
});

describe('parseNovelty', () => {
  it('verdict 只认 novel/known，其余归 uncertain；judged 缺省为 false', () => {
    const out = parseNovelty({
      subclaims: [
        { subclaim: 'a', verdict: 'novel', paper_ids: [], judged: true },
        { subclaim: 'b', verdict: 'whatever' },
      ],
    });
    expect(out).toHaveLength(2);
    expect(out[0]!.verdict).toBe('novel');
    expect(out[1]!).toMatchObject({ verdict: 'uncertain', judged: false });
  });
});

describe('stanceGroups', () => {
  it('三组各归各位', () => {
    const groups = stanceGroups(
      parseGrounding([entry('support', ['p1']), entry('refute', ['p2']), entry('speculation')]),
    );
    expect(groups.support).toHaveLength(1);
    expect(groups.refute).toHaveLength(1);
    expect(groups.speculation).toHaveLength(1);
  });
});

describe('flattenTree', () => {
  const root = node({ id: 'root' });
  const c1 = node({ id: 'c1', parent_id: 'root' });
  const c2 = node({ id: 'c2', parent_id: 'root' });
  const g1 = node({ id: 'g1', parent_id: 'c1' });

  it('先根深度序，子节点缩进一层', () => {
    const out = flattenTree([root, c1, c2, g1], new Set());
    expect(out.map((e) => [e.node.id, e.depth])).toEqual([
      ['root', 0],
      ['c1', 1],
      ['g1', 2],
      ['c2', 1],
    ]);
    expect(out[0]!.hasChildren).toBe(true);
    expect(out[3]!.hasChildren).toBe(false);
  });

  it('collapsed 的节点不展开子树', () => {
    const out = flattenTree([root, c1, c2, g1], new Set(['c1']));
    expect(out.map((e) => e.node.id)).toEqual(['root', 'c1', 'c2']);
  });

  it('父不在集合里的孤儿按根渲染（坏数据也得看得见）', () => {
    const orphan = node({ id: 'o1', parent_id: 'missing' });
    const out = flattenTree([root, orphan], new Set());
    expect(out.map((e) => [e.node.id, e.depth])).toEqual([
      ['root', 0],
      ['o1', 0],
    ]);
  });
});

describe('buildReport', () => {
  it('存活假设 score 降序、未评分排最后、同分按创建时间；剪枝的进附录', () => {
    const nodes = [
      node({ id: 'a', score: 0.2, created_at: '2026-09-01T00:00:02Z' }),
      node({ id: 'b', score: 0.8 }),
      node({ id: 'pruned', score: 0.05, status: 'pruned' }),
      node({ id: 'c', score: null }),
      node({ id: 'd', score: 0.2, created_at: '2026-09-01T00:00:01Z' }),
    ];
    const report = buildReport(nodes);
    expect(report.alive.map((n) => n.id)).toEqual(['b', 'd', 'a', 'c']);
    expect(report.pruned.map((n) => n.id)).toEqual(['pruned']);
  });
});

describe('pruneReasonKind', () => {
  it('低于自动剪枝阈值 → low_score；父被剪 → cascade；否则 decided', () => {
    expect(pruneReasonKind(node({ score: MIN_VIABLE_SCORE - 0.01, status: 'pruned' }), null)).toBe('low_score');
    expect(pruneReasonKind(node({ score: 0.5, status: 'pruned' }), 'pruned')).toBe('cascade');
    expect(pruneReasonKind(node({ score: null, status: 'pruned' }), 'expanded')).toBe('decided');
  });
});
