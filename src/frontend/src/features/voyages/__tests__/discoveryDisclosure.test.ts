import { describe, expect, it } from 'vitest';
import {
  parseDisclosure,
  pruneRecordsOf,
  prunedBranches,
  queriesByPhase,
} from '../discoveryDisclosure';

/* 过程记录纯函数（#655）：产物 schema 不锁死，解析容错与视图变换
   （阶段分组、剪枝原因反查）要钉死——剪枝原因是要给用户看的事实。 */

const ARTIFACT = {
  version: 1,
  queries: [
    { round: 1, phase: 'generate', node_id: 'root', query: 'q-gen', paper_ids: ['p1', 'p2'] },
    { round: 1, phase: 'ground', node_id: 'a', query: 'q-g1', paper_ids: ['p1'] },
    { round: 1, phase: 'novelty', node_id: 'a', query: 'q-n1', paper_ids: ['p2'] },
    { round: 2, phase: 'ground', node_id: 'b', query: 'q-g2', paper_ids: [] },
  ],
  papers: {
    retrieved: ['p1', 'p2', 'p3'],
    cited: ['p1', 'p2'],
    retrieved_not_cited: ['p3'],
    cited_not_retrieved: [],
  },
  branches: [
    {
      node_id: 'root',
      parent_id: null,
      statement: '根假设',
      status: 'expanded',
      score: 0.8,
      timeline: [
        { event: 'created', round: 0 },
        { event: 'expanded', round: 1 },
      ],
    },
    {
      node_id: 'b',
      parent_id: 'root',
      statement: '被剪假设',
      status: 'pruned',
      score: 0.1,
      timeline: [
        { event: 'created', round: 1 },
        { event: 'pruned', round: 2, reason: 'score=0.1 低于阈值，自动剪枝' },
      ],
    },
    {
      node_id: 'ba',
      parent_id: 'b',
      statement: '级联被剪',
      status: 'pruned',
      score: null,
      timeline: [
        { event: 'created', round: 2 },
        { event: 'pruned', round: 2, reason: '随父分支级联剪枝', cascade_from: 'b' },
      ],
    },
  ],
  accounting: { node_usage: {}, total: { prompt_tokens: 17, completion_tokens: 8 } },
  invariants: { cited_subset_of_retrieved: true, pruned_have_reasons: true },
  warnings: [{ code: 'round_without_trace', round: 3 }],
};

describe('parseDisclosure', () => {
  it('完整产物解析出全部区块', () => {
    const d = parseDisclosure(ARTIFACT)!;
    expect(d.queries).toHaveLength(4);
    expect(d.queries[0]).toEqual({
      round: 1,
      phase: 'generate',
      nodeId: 'root',
      query: 'q-gen',
      paperIds: ['p1', 'p2'],
    });
    expect(d.papers.retrievedNotCited).toEqual(['p3']);
    expect(d.branches.map((b) => b.nodeId)).toEqual(['root', 'b', 'ba']);
    expect(d.warnings).toEqual([{ code: 'round_without_trace' }]);
    expect(d.totalTokens).toEqual({ prompt: 17, completion: 8 });
  });

  it('非对象/坏数据降级：整体 null，字段级丢弃不炸', () => {
    expect(parseDisclosure(null)).toBeNull();
    expect(parseDisclosure('x')).toBeNull();
    const d = parseDisclosure({
      queries: [{ phase: 'ground' }, 42, { query: 'ok', phase: '未知', paper_ids: ['p', 1] }],
      branches: [{ statement: '没 id' }, { node_id: 'n', timeline: [{ event: '飞了' }, null] }],
      warnings: [{ nope: 1 }, 'x', { code: 'k' }],
    })!;
    // 没有 query 文本 / 不是对象的条目丢弃；未知阶段归 other，脏 paper_ids 过滤
    expect(d.queries).toEqual([
      { round: null, phase: 'other', nodeId: null, query: 'ok', paperIds: ['p'] },
    ]);
    expect(d.branches).toHaveLength(1);
    expect(d.branches[0]!.timeline).toEqual([]);
    expect(d.warnings).toEqual([{ code: 'k' }]);
    expect(d.totalTokens).toBeNull();
    expect(d.papers.retrieved).toEqual([]);
  });
});

describe('queriesByPhase', () => {
  it('按 生成→接地→查新 定序分组，空阶段不出现，组内保持原序', () => {
    const d = parseDisclosure(ARTIFACT)!;
    const groups = queriesByPhase(d.queries);
    expect(groups.map((g) => g.phase)).toEqual(['generate', 'ground', 'novelty']);
    // 两条 ground（第 1、2 轮）合成一组，顺序不变
    expect(groups[1]!.items.map((q) => q.query)).toEqual(['q-g1', 'q-g2']);
  });

  it('空输入返回空组', () => {
    expect(queriesByPhase([])).toEqual([]);
  });
});

describe('pruneRecordsOf / prunedBranches', () => {
  it('剪枝原因反查表：直接原因带原话，级联带来源', () => {
    const d = parseDisclosure(ARTIFACT)!;
    const records = pruneRecordsOf(d);
    expect(records.size).toBe(2);
    expect(records.get('b')).toEqual({
      reason: 'score=0.1 低于阈值，自动剪枝',
      cascadeFrom: null,
      round: 2,
    });
    expect(records.get('ba')!.cascadeFrom).toBe('b');
    // 没被剪的节点不在表里（组件层据此退回推断）
    expect(records.has('root')).toBe(false);
  });

  it('被剪分支列表保持产物顺序', () => {
    const d = parseDisclosure(ARTIFACT)!;
    expect(prunedBranches(d).map((b) => b.nodeId)).toEqual(['b', 'ba']);
  });

  it('原因缺失（后端 pruned_without_reason 路径）如实为 null', () => {
    const d = parseDisclosure({
      branches: [
        {
          node_id: 'n',
          status: 'pruned',
          statement: 's',
          timeline: [{ event: 'pruned', round: null, reason: null }],
        },
      ],
    })!;
    expect(pruneRecordsOf(d).get('n')).toEqual({ reason: null, cascadeFrom: null, round: null });
  });
});
