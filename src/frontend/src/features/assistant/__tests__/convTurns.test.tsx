import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { applyAssistantEvent, type AssistantBlock } from '../../../lib/assistantStream';
import {
  keepLocalTurns,
  patchConvTurn,
  pruneConvTurns,
  renameConv,
  turnsOf,
  updateConvTurns,
  type Turn,
  type TurnsByConv,
} from '../convTurns';
import { NoticeView, errorBlock, errorText } from '../ErrorNotice';

/* ============================================================
   #850：流里的帧按会话分开写；切会话不把别的会话的内容带过去。
   ============================================================ */

const ask = (q: string): Turn[] => [
  { role: 'user', blocks: [{ kind: 'text', text: q }] },
  { role: 'assistant', blocks: [] },
];

/** 一条流的帧：只写它所属的会话 */
const frame = (m: TurnsByConv, conv: string, event: string, data: unknown) =>
  patchConvTurn(m, conv, (blocks) => applyAssistantEvent(blocks, event, data));

const tools = (turns: Turn[]) =>
  turns.flatMap((t) => t.blocks).filter((b): b is Extract<AssistantBlock, { kind: 'tool' }> => b.kind === 'tool');

describe('turns per conversation', () => {
  it('a frame stays in its own conversation after switching away', () => {
    // A 在跑；用户切到 B（B 从服务端载入，最后一轮是助手轮）
    let m: TurnsByConv = updateConvTurns({}, 'A', () => ask('find papers'));
    m = { ...m, B: [...ask('hello'), { role: 'user', blocks: [{ kind: 'text', text: 'again' }] }, { role: 'assistant', blocks: [{ kind: 'text', text: 'hi' }] }] };
    const before = turnsOf(m, 'B');
    // 切走之后 A 的流才来工具调用
    m = frame(m, 'A', 'tool_call', { id: 't1', name: 'search_papers', args: {} });
    expect(turnsOf(m, 'B')).toBe(before);
    expect(tools(turnsOf(m, 'B'))).toEqual([]);
    expect(tools(turnsOf(m, 'A'))).toHaveLength(1);
  });

  it('switching back to a running conversation keeps the in-memory turn', () => {
    let m: TurnsByConv = updateConvTurns({}, 'A', () => ask('find papers'));
    m = frame(m, 'A', 'tool_call', { id: 't1', name: 'search_papers', args: {} });
    const running = new Set(['A']);
    // 切到 B：只留在跑的 A 和正在看的 B
    m = pruneConvTurns({ ...m, B: ask('other'), C: ask('old') }, new Set([...running, 'B']));
    expect(Object.keys(m).sort()).toEqual(['A', 'B']);
    // 切回 A：它还在跑，用内存里那份，不从服务端重建（这一轮还没落库）
    expect(keepLocalTurns(m, 'A', running)).toBe(true);
    expect(tools(turnsOf(m, 'A'))[0]!.state).toBe('running');
    // 已经跑完的会话照常从服务端拉
    expect(keepLocalTurns(m, 'A', new Set())).toBe(false);
    expect(keepLocalTurns(m, 'C', running)).toBe(false);
  });

  it('a new conversation keeps its turns when it gets its real id', () => {
    const m = updateConvTurns({}, '__new__', () => ask('first'));
    const renamed = renameConv(m, '__new__', 'c1');
    expect(renamed.__new__).toBeUndefined();
    expect(turnsOf(renamed, 'c1')).toHaveLength(2);
    expect(renameConv(renamed, '__new__', 'c2')).toBe(renamed);
  });

  it('ignores frames when the last turn is not an answer', () => {
    const m: TurnsByConv = { A: [{ role: 'user', blocks: [] }] };
    expect(frame(m, 'A', 'delta', { text: 'x' })).toBe(m);
    const empty: TurnsByConv = {};
    expect(frame(empty, 'A', 'delta', { text: 'x' })).toBe(empty);
  });

  it('drops the external-agent approval frames that are no longer sent', () => {
    // 旧引擎还可能发这两帧：认不出就原样返回，不画卡片也不抛
    const blocks: AssistantBlock[] = [{ kind: 'text', text: 'working' }];
    const request = { request_id: 'r1', options: [{ id: 'o1', name: 'Allow', kind: 'allow_once' }] };
    expect(applyAssistantEvent(blocks, 'permission_request', request)).toBe(blocks);
    expect(applyAssistantEvent(blocks, 'permission_resolved', { request_id: 'r1', outcome: 'allowed' })).toBe(blocks);
  });
});

describe('error notices', () => {
  it('the missing-key error says how to provide it and links to the model status', () => {
    const block = errorBlock('LLM_NOT_CONFIGURED');
    expect(block.action).toBe('model-settings');
    expect(block.text).toContain('API key');
    expect(block.text).toContain('ANTHROPIC_API_KEY');
    expect(block.text).not.toContain('](');
    // 模型不可配置：不再让人去「添加模型」或「添加智能体」
    expect(block.text).not.toMatch(/智能体|模型服务|添加/);
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <NoticeView block={block} />
      </MemoryRouter>,
    );
    expect(html).toMatch(/<a [^>]*href="\/settings\?tab=model"[^>]*>查看模型状态<\/a>/);
    expect(html).not.toContain('tab=llm');
  });

  it('recognises the same error in its other spellings', () => {
    const text = errorText('LLM_NOT_CONFIGURED');
    expect(errorText('Error: LLMNotConfiguredError')).toBe(text);
    expect(errorBlock('No model configured for stage default').action).toBe('model-settings');
  });

  it('other errors carry no link and keep the raw detail when asked', () => {
    const block = errorBlock('boom', true);
    expect(block.action).toBeUndefined();
    // 认不出的错误原样就是正文，不再重复一遍
    expect(block.text).toBe('boom');
    expect(block.detail).toBeUndefined();
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <NoticeView block={errorBlock('Error: something broke', true)} />
      </MemoryRouter>,
    );
    expect(html).not.toContain('<a ');
    expect(html).toContain('something broke');
  });
});
