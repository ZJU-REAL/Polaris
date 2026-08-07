import { describe, expect, it } from 'vitest';
import { restoreBlocks } from '../AssistantPanel';
import type { AssistantBlock } from '../../../lib/assistantStream';

/* ============================================================
   回放历史会话时，工具调用与它的结果分属**两条消息**：assistant 那条带 tool_use，
   紧跟的 tool_results 那条带 tool_result。

   这条测试的由来：卡片表原先建在 restoreBlocks 内部，每条消息重建一次，于是回放
   第二条时压根找不到第一条的卡片。后果有两个，都在界面上看得见——卡片永远拿不到
   summary，标签就一直写着「调用中…」（图标却已经是 ✓）；而那条消息一个块都没产出，
   触发兜底，把 `[工具结果] {"summary": ...}` 整段原始 JSON 当正文打进对话里。
   ============================================================ */

type Card = Extract<AssistantBlock, { kind: 'tool' }>;

const toolUseMessage = {
  kind: 'normal',
  text: '',
  blocks: [{ kind: 'tool_use', id: 'call_1', name: 'get_experiment' }],
};

const toolResultMessage = {
  kind: 'tool_results',
  // 落库时这条的 text 是把结果拍平成的样子（core/llm/base.py _block_text）
  text: '[工具结果] {"summary": "实验详情：setup", "preview": "{\\"id\\": \\"e815...\\"}"}',
  blocks: [
    {
      kind: 'tool_result',
      tool_use_id: 'call_1',
      content: JSON.stringify({
        summary: '实验详情：setup',
        preview: '{"id": "e815..."}',
        duration_ms: 11,
        ok: true,
      }),
    },
  ],
};

describe('回放一轮工具调用', () => {
  it('结果能跨消息回填到卡片上', () => {
    const cardById = new Map<string, Card>();
    const first = restoreBlocks(toolUseMessage, cardById);
    const second = restoreBlocks(toolResultMessage, cardById);

    const card = first[0] as Card;
    expect(card.kind).toBe('tool');
    expect(card.name).toBe('get_experiment');
    // 回填到的是第一条里那张卡，所以第二条不该再产出块
    expect(card.summary).toBe('实验详情：setup');
    expect(card.durationMs).toBe(11);
    expect(second).toEqual([]);
  });

  it('绝不把工具结果的原始 JSON 当正文打出来', () => {
    const cardById = new Map<string, Card>();
    restoreBlocks(toolUseMessage, cardById);
    const second = restoreBlocks(toolResultMessage, cardById);
    expect(JSON.stringify(second)).not.toContain('[工具结果]');
  });

  it('即使结果成了孤儿，也不倒出原始 JSON', () => {
    // 存量数据或落库出错时，tool_result 可能找不到对应的 tool_use。
    // 宁可少一张卡，也不要把几万字符的原始结果糊到用户脸上。
    const orphan = restoreBlocks(toolResultMessage, new Map<string, Card>());
    expect(orphan).toEqual([]);
  });

  it('真的有话要说的消息仍然走兜底', () => {
    // 兜底本身是有用的：没有块、但有正文的消息，正文得显示出来
    const plain = restoreBlocks(
      { kind: 'normal', text: '好的，我来看看。', blocks: [] },
      new Map<string, Card>(),
    );
    expect(plain).toEqual([{ kind: 'text', text: '好的，我来看看。' }]);
  });
});
