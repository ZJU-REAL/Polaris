import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  applyAssistantEvent,
  setPermissionState,
  type AssistantBlock,
  type PermissionBlock,
} from '../../../lib/assistantStream';
import { PermissionCard, permissionOptionLabel } from '../PermissionCard';
import { phaseOf } from '../TurnStatus';

/* ============================================================
   外部 agent 的授权请求（#838）：流 → 授权卡 → 状态行。
   ============================================================ */

const request = {
  request_id: 'r1',
  tool_id: 't1',
  title: 'Run npm test',
  kind: 'execute',
  input: '{"command":"npm test"}',
  options: [
    { id: 'o1', name: 'Allow', kind: 'allow_once' },
    { id: 'o2', name: '', kind: 'allow_always' },
    { id: 'o3', name: 'Reject', kind: 'reject_once' },
  ],
  timeout_s: 300,
};

function reduce(events: [string, unknown][]): AssistantBlock[] {
  let blocks: AssistantBlock[] = [];
  for (const [event, data] of events) blocks = applyAssistantEvent(blocks, event, data);
  return blocks;
}

function permission(blocks: AssistantBlock[]): PermissionBlock {
  const b = blocks.find((x) => x.kind === 'permission');
  if (!b || b.kind !== 'permission') throw new Error('no permission block');
  return b;
}

describe('permission frames', () => {
  it('permission_request appends a pending card', () => {
    const b = permission(reduce([['permission_request', request]]));
    expect(b).toMatchObject({
      requestId: 'r1',
      toolId: 't1',
      title: 'Run npm test',
      toolKind: 'execute',
      input: '{"command":"npm test"}',
      timeoutS: 300,
      state: 'pending',
    });
    expect(b.options.map((o) => o.kind)).toEqual(['allow_once', 'allow_always', 'reject_once']);
    expect(typeof b.receivedAt).toBe('number');
  });

  it('permission_resolved sets the outcome', () => {
    const allowed = reduce([
      ['permission_request', request],
      ['permission_resolved', { request_id: 'r1', outcome: 'allowed' }],
    ]);
    expect(permission(allowed).state).toBe('allowed');
    const denied = reduce([
      ['permission_request', request],
      ['permission_resolved', { request_id: 'r1', outcome: 'denied' }],
    ]);
    expect(permission(denied).state).toBe('denied');
  });

  it('ignores malformed frames', () => {
    expect(reduce([['permission_request', null]])).toEqual([]);
    expect(reduce([['permission_request', { ...request, request_id: '' }]])).toEqual([]);
    expect(reduce([['permission_request', { ...request, options: 'nope' }]])).toEqual([]);
    expect(reduce([['permission_request', { ...request, options: [{ id: 'x', kind: 'maybe' }, null] }]])).toEqual([]);
    // 缺字段兜底，不抛
    const sparse = permission(reduce([['permission_request', { request_id: 'r2', options: [{ id: 'a', kind: 'allow_once' }] }]]));
    expect(sparse).toMatchObject({ title: '', toolKind: 'other', input: '', timeoutS: 300, state: 'pending' });

    const base = reduce([['permission_request', request]]);
    expect(applyAssistantEvent(base, 'permission_resolved', { request_id: 'r1', outcome: 'maybe' })).toBe(base);
    expect(applyAssistantEvent(base, 'permission_resolved', { request_id: 'other', outcome: 'allowed' })).toBe(base);
    expect(applyAssistantEvent(base, 'permission_resolved', 'garbage')).toBe(base);
    // 同一个请求来两次只画一张
    expect(applyAssistantEvent(base, 'permission_request', request)).toBe(base);
  });

  it('local answers only move the card from the expected state', () => {
    const base = reduce([['permission_request', request]]);
    const answering = setPermissionState(base, 'r1', 'answering', { from: 'pending' });
    expect(permission(answering).state).toBe('answering');
    // 流里的 resolved 先到了：本地回调不该再把它改回去
    const resolved = applyAssistantEvent(answering, 'permission_resolved', { request_id: 'r1', outcome: 'denied' });
    expect(setPermissionState(resolved, 'r1', 'pending', { from: 'answering' })).toBe(resolved);
    const expired = setPermissionState(answering, 'r1', 'denied', { expired: true, from: 'answering' });
    expect(permission(expired)).toMatchObject({ state: 'denied', expired: true });
  });

  it('the turn status says it is waiting for approval', () => {
    const pending = reduce([
      ['tool_call', { id: 't1', name: 'agent_execute', args: { title: 'Run npm test' } }],
      ['permission_request', request],
    ]);
    expect(phaseOf(pending, true)).toBe('approval');
    const done = applyAssistantEvent(pending, 'permission_resolved', { request_id: 'r1', outcome: 'allowed' });
    expect(phaseOf(done, true)).toBe('tool');
    expect(phaseOf(pending, false)).toBe('idle');
  });
});

describe('permission card', () => {
  const block = permission(reduce([['permission_request', request]]));
  const noop = () => undefined;

  it('labels options by name, falling back to the kind', () => {
    expect(permissionOptionLabel(block.options[0]!)).toBe('Allow');
    expect(permissionOptionLabel(block.options[1]!)).toBe('总是允许');
  });

  it('renders a pending request with its buttons and the timeout hint', () => {
    const html = renderToStaticMarkup(
      <PermissionCard block={block} conversationId="c1" turnLive onStateChange={noop} />,
    );
    expect(html).toContain('需要你批准');
    expect(html).toContain('运行命令');
    expect(html).toContain('Run npm test');
    expect(html).toContain('&quot;command&quot;: &quot;npm test&quot;');
    expect(html).toContain('总是允许');
    expect(html).toContain('5 分钟内不回答就按拒绝处理');
    expect(html.match(/<button/g)?.length).toBe(3);
    expect(html).toContain('btn btn-primary sm');
    expect(html).toContain('btn btn-ghost sm');
  });

  it('collapses to one line once resolved', () => {
    const allowed = renderToStaticMarkup(
      <PermissionCard block={{ ...block, state: 'allowed' }} conversationId="c1" turnLive onStateChange={noop} />,
    );
    expect(allowed).toContain('已允许');
    expect(allowed).not.toContain('<button');
    const expired = renderToStaticMarkup(
      <PermissionCard
        block={{ ...block, state: 'denied', expired: true }}
        conversationId="c1"
        turnLive
        onStateChange={noop}
      />,
    );
    expect(expired).toContain('已拒绝');
    expect(expired).toContain('请求已过期');
  });

  it('drops the buttons when the turn has ended', () => {
    const html = renderToStaticMarkup(
      <PermissionCard block={block} conversationId="c1" turnLive={false} onStateChange={noop} />,
    );
    expect(html).not.toContain('<button');
    expect(html).toContain('这一轮已经结束');
  });
});
