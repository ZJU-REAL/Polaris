import { describe, expect, it } from 'vitest';
import { applyAssistantEvent, type AssistantBlock } from '../../../lib/assistantStream';

/* ============================================================
   事件流 → 块时间线。

   这段代码此前一个测试都没有，而它处理的恰好是**最不可信的输入**：SSE 里
   JSON.parse 出来的 any。TypeScript 在这儿帮不上忙——编译期它以为自己知道形状，
   运行时后端发什么就是什么。所以这里主要钉的不是"正常情况对不对"，而是
   "畸形输入会不会把界面炸掉"。
   ============================================================ */

/** 把一串事件按顺序喂进去，返回最终的块列表。 */
function reduce(events: [string, unknown][]): AssistantBlock[] {
  let blocks: AssistantBlock[] = [];
  for (const [event, data] of events) {
    blocks = applyAssistantEvent(blocks, event, data);
  }
  return blocks;
}

describe('正文增量', () => {
  it('连续的 delta 拼进同一个文本块', () => {
    const blocks = reduce([
      ['delta', { text: '这篇' }],
      ['delta', { text: '论文' }],
    ]);
    expect(blocks).toEqual([{ kind: 'text', text: '这篇论文' }]);
  });

  it('工具卡片之后的 delta 另起一块，不会把答案接到卡片前面的文字上', () => {
    const blocks = reduce([
      ['delta', { text: '先查一下' }],
      ['tool_call', { id: 't1', name: 'search_papers' }],
      ['delta', { text: '查到了' }],
    ]);
    expect(blocks.map((b) => b.kind)).toEqual(['text', 'tool', 'text']);
  });

  it('空 delta 不产生空块', () => {
    expect(reduce([['delta', { text: '' }]])).toEqual([]);
  });
});

describe('工具卡片', () => {
  it('结果按 id 回填到对应的卡片上', () => {
    const blocks = reduce([
      ['tool_call', { id: 'a', name: 'search_papers' }],
      ['tool_call', { id: 'b', name: 'get_paper' }],
      ['tool_result', { id: 'b', ok: true, summary: '拿到了', duration_ms: 12 }],
    ]);
    expect(blocks[0]).toMatchObject({ id: 'a', state: 'running' });
    expect(blocks[1]).toMatchObject({ id: 'b', state: 'ok', summary: '拿到了', durationMs: 12 });
  });

  it('认不出的 id 不会误伤别的卡片', () => {
    const blocks = reduce([
      ['tool_call', { id: 'a', name: 'search_papers' }],
      ['tool_result', { id: '不存在', ok: false }],
    ]);
    expect(blocks[0]).toMatchObject({ id: 'a', state: 'running' });
  });

  it('ok=false 画成失败态', () => {
    const blocks = reduce([
      ['tool_call', { id: 'a', name: 'x' }],
      ['tool_result', { id: 'a', ok: false }],
    ]);
    expect(blocks[0]).toMatchObject({ state: 'error' });
  });
});

describe('计划', () => {
  it('计划是替换不是追加——界面上永远只有一份最新的', () => {
    const blocks = reduce([
      ['plan', { steps: [{ title: '查', status: 'running' }] }],
      ['plan', { steps: [{ title: '查', status: 'done' }, { title: '读', status: 'running' }] }],
    ]);
    expect(blocks.filter((b) => b.kind === 'plan')).toHaveLength(1);
    expect(blocks[0]).toEqual({
      kind: 'plan',
      steps: [
        { title: '查', status: 'done' },
        { title: '读', status: 'running' },
      ],
    });
  });

  it('没有标题的步骤丢掉，认不出的状态退回 pending', () => {
    const blocks = reduce([
      ['plan', { steps: [{ title: '', status: 'done' }, { title: '读', status: '在做' }] }],
    ]);
    expect(blocks[0]).toEqual({ kind: 'plan', steps: [{ title: '读', status: 'pending' }] });
  });

  it('一步都不剩时不画空进度条', () => {
    expect(reduce([['plan', { steps: [] }]])).toEqual([]);
    expect(reduce([['plan', { steps: '不是数组' }]])).toEqual([]);
  });
});

describe('畸形输入', () => {
  it('未知事件忽略：后端加一个新事件不该让旧前端崩掉', () => {
    expect(reduce([['某个将来才有的事件', { whatever: 1 }]])).toEqual([]);
  });

  it('字段缺失、类型不对都不抛', () => {
    const cases: [string, unknown][] = [
      ['delta', null],
      ['delta', { text: 123 }],
      ['thinking', {}],
      ['tool_call', {}],
      ['tool_result', undefined],
      ['plan', { steps: [null, 42, { title: 7 }] }],
    ];
    for (const [event, data] of cases) {
      expect(() => applyAssistantEvent([], event, data)).not.toThrow();
    }
  });
});

describe('本轮查看的论文', () => {
  it('后端只发新出现的，这里追加并去重', () => {
    const blocks = reduce([
      ['sources', { items: [{ paper_id: 'a', title: '甲' }] }],
      ['sources', { items: [{ paper_id: 'a', title: '甲' }, { paper_id: 'b', title: '乙' }] }],
    ]);
    const sources = blocks.filter((b) => b.kind === 'sources');
    expect(sources).toHaveLength(1);
    expect(sources[0]).toEqual({
      kind: 'sources',
      papers: [
        { paperId: 'a', title: '甲' },
        { paperId: 'b', title: '乙' },
      ],
    });
  });

  it('缺 id 或缺标题的条目丢掉——一串 uuid 在界面上没法看', () => {
    const blocks = reduce([
      ['sources', { items: [{ paper_id: 'a' }, { title: '没有 id' }, { paper_id: 'b', title: '乙' }] }],
    ]);
    expect(blocks[0]).toEqual({ kind: 'sources', papers: [{ paperId: 'b', title: '乙' }] });
  });

  it('一条都认不出时不留空块', () => {
    expect(reduce([['sources', { items: [] }]])).toEqual([]);
    expect(reduce([['sources', { items: '不是数组' }]])).toEqual([]);
  });
});

describe('验收结论', () => {
  it('通过时不画任何东西——绿条只会训练用户忽略这一栏', () => {
    expect(reduce([['verify', { passed: true, notes: [] }]])).toEqual([]);
    expect(reduce([['verify', { passed: true, notes: ['理论上不该有'] }]])).toEqual([]);
  });

  it('没通过时把话说出来', () => {
    const blocks = reduce([['verify', { passed: false, notes: ['引用编号超出查到的篇数'] }]]);
    expect(blocks).toEqual([{ kind: 'verify', notes: ['引用编号超出查到的篇数'] }]);
  });

  it('没有具体说法就不占地方', () => {
    expect(reduce([['verify', { passed: false, notes: [] }]])).toEqual([]);
  });
});

describe('工具返回的图片', () => {
  it('tool_result 里的图片出处要装进卡片', () => {
    const blocks = reduce([
      ['tool_call', { id: 'f1', name: 'get_paper_figure' }],
      [
        'tool_result',
        {
          id: 'f1',
          ok: true,
          image_refs: [{ kind: 'paper_figure', paper_id: 'p-1', index: 3, label: '图注' }],
        },
      ],
    ]);
    expect(blocks[0]).toMatchObject({
      kind: 'tool',
      images: [{ kind: 'paper_figure', paperId: 'p-1', index: 3, label: '图注' }],
    });
  });

  it('认不出的出处丢掉，一张都不剩时是 undefined 而不是空数组', () => {
    const blocks = reduce([
      ['tool_call', { id: 'f1', name: 'get_paper_figure' }],
      [
        'tool_result',
        {
          id: 'f1',
          ok: true,
          image_refs: [{ kind: '未来的类型', paper_id: 'p' }, { paper_id: 'p' }, { index: 1 }],
        },
      ],
    ]);
    expect((blocks[0] as { images?: unknown }).images).toBeUndefined();
  });

  it('没有图片的工具结果不会凭空长出 images 字段', () => {
    const blocks = reduce([
      ['tool_call', { id: 's1', name: 'search_papers' }],
      ['tool_result', { id: 's1', ok: true, summary: '3 篇' }],
    ]);
    expect((blocks[0] as { images?: unknown }).images).toBeUndefined();
  });
});

describe('待审批的计划', () => {
  it('submit_plan 交上来的带审批标记，推进中的不带', () => {
    // 同一种块两种读法：带标记的是审批单（画「按这个做 / 改一改」），
    // 不带的是进度条。推进中的计划要是也带一个 false，每处比较都得多写一笔。
    const pending = applyAssistantEvent([], 'plan', {
      steps: [{ title: '查文献', status: 'pending' }],
      awaiting_approval: true,
    });
    expect(pending[0]).toEqual({
      kind: 'plan',
      steps: [{ title: '查文献', status: 'pending' }],
      awaitingApproval: true,
    });

    const running = applyAssistantEvent([], 'plan', {
      steps: [{ title: '查文献', status: 'running' }],
    });
    expect(running[0]).not.toHaveProperty('awaitingApproval');
  });
});
