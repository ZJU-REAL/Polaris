import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { applyAssistantEvent, type AssistantBlock } from '../../../lib/assistantStream';
import {
  keepLocalTurns,
  patchConvTurn,
  pruneConvTurns,
  renameConv,
  setPermissionAcross,
  turnsOf,
  updateConvTurns,
  type Turn,
  type TurnsByConv,
} from '../convTurns';
import { NoticeView, errorBlock, errorText } from '../ErrorNotice';
import { PermissionCard } from '../PermissionCard';
import { listboxKey } from '../listboxKeys';

/* ============================================================
   #850：流里的帧按会话分开写；切会话不把授权卡带过去。
   ============================================================ */

const request = {
  request_id: 'r1',
  tool_id: 't1',
  title: 'Run npm test',
  kind: 'execute',
  input: '{"command":"npm test"}',
  options: [
    { id: 'o1', name: 'Allow', kind: 'allow_once' },
    { id: 'o3', name: 'Reject', kind: 'reject_once' },
  ],
  timeout_s: 300,
};

const ask = (q: string): Turn[] => [
  { role: 'user', blocks: [{ kind: 'text', text: q }] },
  { role: 'assistant', blocks: [] },
];

/** 一条流的帧：只写它所属的会话 */
const frame = (m: TurnsByConv, conv: string, event: string, data: unknown) =>
  patchConvTurn(m, conv, (blocks) => applyAssistantEvent(blocks, event, data));

const permissions = (turns: Turn[]) =>
  turns.flatMap((t) => t.blocks).filter((b): b is Extract<AssistantBlock, { kind: 'permission' }> => b.kind === 'permission');

describe('turns per conversation', () => {
  it('a permission request stays in its own conversation after switching away', () => {
    // A 在跑，等授权；用户切到 B（B 从服务端载入，最后一轮是助手轮）
    let m: TurnsByConv = updateConvTurns({}, 'A', () => ask('run the tests'));
    m = frame(m, 'A', 'tool_call', { id: 't1', name: 'agent_execute', args: { title: 'Run npm test' } });
    m = { ...m, B: [...ask('hello'), { role: 'user', blocks: [{ kind: 'text', text: 'again' }] }, { role: 'assistant', blocks: [{ kind: 'text', text: 'hi' }] }] };
    const before = turnsOf(m, 'B');
    // 切走之后 A 的流才来授权请求
    m = frame(m, 'A', 'permission_request', request);
    expect(turnsOf(m, 'B')).toBe(before);
    expect(permissions(turnsOf(m, 'B'))).toEqual([]);
    expect(permissions(turnsOf(m, 'A'))).toHaveLength(1);
  });

  it('switching back to a running conversation keeps the in-memory card, which is still actionable', () => {
    let m: TurnsByConv = updateConvTurns({}, 'A', () => ask('run the tests'));
    m = frame(m, 'A', 'permission_request', request);
    const running = new Set(['A']);
    // 切到 B：只留在跑的 A 和正在看的 B
    m = pruneConvTurns({ ...m, B: ask('other') }, new Set([...running, 'B']));
    expect(Object.keys(m).sort()).toEqual(['A', 'B']);
    // 切回 A：它还在跑，用内存里那份，不从服务端重建（服务端不存授权请求）
    expect(keepLocalTurns(m, 'A', running)).toBe(true);
    const card = permissions(turnsOf(m, 'A'))[0]!;
    expect(card.state).toBe('pending');
    const html = renderToStaticMarkup(
      <PermissionCard block={card} conversationId="A" turnLive onStateChange={() => undefined} />,
    );
    expect(html.match(/<button/g)?.length).toBe(2);
    // 已经跑完的会话照常从服务端拉
    expect(keepLocalTurns(m, 'A', new Set())).toBe(false);
    expect(keepLocalTurns(m, 'C', running)).toBe(false);
  });

  it('answering a card updates it wherever it lives', () => {
    let m: TurnsByConv = updateConvTurns({}, 'A', () => ask('x'));
    m = frame(m, 'A', 'permission_request', request);
    m = { ...m, B: ask('y') };
    const b = m.B;
    m = setPermissionAcross(m, 'r1', 'answering', { from: 'pending' });
    expect(permissions(turnsOf(m, 'A'))[0]!.state).toBe('answering');
    expect(m.B).toBe(b);
    expect(setPermissionAcross(m, 'nope', 'allowed')).toBe(m);
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
    expect(frame(m, 'A', 'permission_request', request)).toBe(m);
    const empty: TurnsByConv = {};
    expect(frame(empty, 'A', 'permission_request', request)).toBe(empty);
  });
});

describe('error notices', () => {
  it('the no-model error renders a real in-app link to settings', () => {
    const block = errorBlock('LLM_NOT_CONFIGURED');
    expect(block.action).toBe('model-settings');
    expect(block.text).not.toContain('](');
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <NoticeView block={block} />
      </MemoryRouter>,
    );
    expect(html).toMatch(/<a [^>]*href="\/settings\?tab=llm"[^>]*>去设置<\/a>/);
    expect(html).not.toContain('[去设置]');
  });

  it('other errors carry no link and keep the raw detail when asked', () => {
    const block = errorBlock('boom', true);
    expect(block.action).toBeUndefined();
    // 认不出的错误原样就是正文，不再重复一遍
    expect(block.text).toBe('boom');
    expect(block.detail).toBeUndefined();
    expect(errorBlock('ACP_AGENT_NOT_AVAILABLE', true).detail).toBe('ACP_AGENT_NOT_AVAILABLE');
    expect(errorBlock('ACP_AGENT_NOT_AVAILABLE').detail).toBeUndefined();
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <NoticeView block={errorBlock('Error: ACP_AGENT_NOT_AVAILABLE', true)} />
      </MemoryRouter>,
    );
    expect(html).not.toContain('<a ');
    expect(html).toContain('ACP_AGENT_NOT_AVAILABLE');
  });

  it('no longer talks about sharing agents', () => {
    expect(errorText('ACP_AGENT_NOT_AVAILABLE')).not.toContain('共享');
    expect(errorText('ACP_AGENT_NOT_AVAILABLE')).toContain('删掉或停用');
  });
});

describe('backend picker keys', () => {
  it('moves with the arrows, wraps, and closes on Escape', () => {
    expect(listboxKey('ArrowDown', -1, 3)).toEqual({ focus: 0 });
    expect(listboxKey('ArrowDown', 2, 3)).toEqual({ focus: 0 });
    expect(listboxKey('ArrowUp', 0, 3)).toEqual({ focus: 2 });
    expect(listboxKey('ArrowUp', -1, 3)).toEqual({ focus: 2 });
    expect(listboxKey('Home', 1, 3)).toEqual({ focus: 0 });
    expect(listboxKey('End', 0, 3)).toEqual({ focus: 2 });
    expect(listboxKey('Escape', 1, 3)).toBe('close');
    expect(listboxKey('a', 1, 3)).toBeNull();
    expect(listboxKey('ArrowDown', -1, 0)).toBeNull();
  });
});
