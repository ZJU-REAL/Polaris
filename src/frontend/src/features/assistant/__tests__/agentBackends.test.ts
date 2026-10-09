import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { applyAssistantEvent, type AssistantBlock } from '../../../lib/assistantStream';
import { agentToolIcon, agentToolKind, agentToolLabel, toolDisplayName } from '../agentTools';
import { POLARIS_BACKEND, readBackendChoice, resolveBackend, writeBackendChoice } from '../backendChoice';

function memoryStore() {
  const data = new Map<string, string>();
  return {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => void data.set(k, v),
  };
}

describe('agent tool cards', () => {
  it('recognises agent_<kind> names and leaves Polaris tools alone', () => {
    expect(agentToolKind('agent_read')).toBe('read');
    expect(agentToolKind('agent_execute')).toBe('execute');
    expect(agentToolKind('agent_mystery')).toBe('other');
    expect(agentToolKind('search_papers')).toBeNull();
    expect(agentToolIcon('edit')).toBe('pen');
    expect(agentToolLabel('read')).toBe('读取');
  });

  it('shows the title, then the summary, then the kind label', () => {
    expect(toolDisplayName({ name: 'agent_read', title: 'Read notes.txt' })).toBe('Read notes.txt');
    expect(toolDisplayName({ name: 'agent_read', summary: 'Read /a/b.txt' })).toBe('Read /a/b.txt');
    expect(toolDisplayName({ name: 'agent_search' })).toBe('搜索');
    expect(toolDisplayName({ name: 'search_papers', title: 'ignored', summary: '3 hits' })).toBe('search_papers');
  });

  it('keeps the title from agent tool calls only', () => {
    const agent = applyAssistantEvent([], 'tool_call', {
      id: 't1',
      name: 'agent_read',
      args: { title: 'Read notes.txt', input: {} },
    });
    expect(agent[0]).toEqual({ kind: 'tool', id: 't1', name: 'agent_read', title: 'Read notes.txt', state: 'running' });
    const polaris: AssistantBlock[] = applyAssistantEvent([], 'tool_call', {
      id: 't2',
      name: 'search_papers',
      args: { title: 'not for people' },
    });
    expect(polaris[0]).toEqual({ kind: 'tool', id: 't2', name: 'search_papers', state: 'running' });
  });
});

describe('backend choice per conversation', () => {
  it('remembers per conversation and uses the last pick for new ones', () => {
    const s = memoryStore();
    expect(readBackendChoice(null, null, s)).toEqual({ choice: POLARIS_BACKEND, explicit: false });
    writeBackendChoice('c1', 'agent-a', s);
    expect(readBackendChoice('c1', null, s)).toEqual({ choice: 'agent-a', explicit: true });
    expect(readBackendChoice(null, null, s)).toEqual({ choice: 'agent-a', explicit: true });
    writeBackendChoice('c2', POLARIS_BACKEND, s);
    expect(readBackendChoice('c1', null, s).choice).toBe('agent-a');
    expect(readBackendChoice('c2', null, s).choice).toBe(POLARIS_BACKEND);
  });

  it('uses the server value for conversations never picked here, and does not resend it', () => {
    const s = memoryStore();
    // 新会话默认值是 agent-a，但老会话 c-old 在服务端存的是 agent-b
    writeBackendChoice(null, 'agent-a', s);
    expect(readBackendChoice('c-old', 'agent-b', s)).toEqual({ choice: 'agent-b', explicit: false });
    // 服务端也没存：显示 Polaris，同样不发
    expect(readBackendChoice('c-older', null, s)).toEqual({ choice: POLARIS_BACKEND, explicit: false });
    // 本地亲手选过的优先于服务端
    writeBackendChoice('c-old', 'agent-c', s);
    expect(readBackendChoice('c-old', 'agent-b', s)).toEqual({ choice: 'agent-c', explicit: true });
  });

  it('survives broken storage', () => {
    const broken = {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('blocked');
      },
    };
    expect(readBackendChoice('c1', null, broken).choice).toBe(POLARIS_BACKEND);
    expect(readBackendChoice(null, null, broken)).toEqual({ choice: POLARIS_BACKEND, explicit: false });
    expect(() => writeBackendChoice('c1', 'x', broken)).not.toThrow();
    expect(readBackendChoice('c1', 'agent-b', null).choice).toBe('agent-b');
  });

  it('falls back to Polaris when the picked agent is gone', () => {
    const list = [{ id: 'polaris' }, { id: 'agent-a' }];
    expect(resolveBackend('agent-a', list)).toBe('agent-a');
    expect(resolveBackend('agent-gone', list)).toBe(POLARIS_BACKEND);
  });

  it('the panel only sends an explicit pick and hides the picker when only Polaris exists', () => {
    const src = readFileSync(fileURLToPath(new URL('../AssistantPanel.tsx', import.meta.url)), 'utf-8');
    expect(src).toContain('backends.length > 1');
    expect(src).toContain('const sendBackend = backendChoice.explicit ? backend : undefined;');
    expect(src).toMatch(/\n\s+backend: sendBackend,\n\s+onMeta:/);
    expect(src).toContain('setServerBackend(row?.backend ?? null)');
  });
});
