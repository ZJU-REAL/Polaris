import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { applyAssistantEvent, type AssistantBlock } from '../../../lib/assistantStream';
import { agentToolIcon, agentToolKind, agentToolLabel, toolDisplayName } from '../agentTools';
import {
  LOCAL_AGENTS_HREF,
  POLARIS_BACKEND,
  backendLabel,
  readBackendChoice,
  resolveBackend,
  writeBackendChoice,
} from '../backendChoice';
import { NoticeView, agentErrorText, errorBlock } from '../ErrorNotice';
import { listboxKey } from '../listboxKeys';

/* ============================================================
   助手里的「由谁回答」：Polaris · Claude（默认），或一个本机智能体。
   ============================================================ */

function memoryStore() {
  const data = new Map<string, string>();
  return {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => void data.set(k, v),
  };
}

const panelSource = readFileSync(fileURLToPath(new URL('../AssistantPanel.tsx', import.meta.url)), 'utf-8');

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

describe('who answers, per conversation', () => {
  it('starts every new conversation on Polaris, and remembers a pick per conversation', () => {
    const s = memoryStore();
    expect(readBackendChoice(null, null, s)).toEqual({ choice: POLARIS_BACKEND, explicit: false });
    writeBackendChoice('c1', 'agent-a', s);
    expect(readBackendChoice('c1', null, s)).toEqual({ choice: 'agent-a', explicit: true });
    // 上一场选了智能体，新的一场不跟着走
    expect(readBackendChoice(null, null, s)).toEqual({ choice: POLARIS_BACKEND, explicit: false });
    writeBackendChoice('c2', POLARIS_BACKEND, s);
    expect(readBackendChoice('c1', null, s).choice).toBe('agent-a');
    expect(readBackendChoice('c2', null, s).choice).toBe(POLARIS_BACKEND);
    // 新对话还没有 id：不记
    writeBackendChoice(null, 'agent-z', s);
    expect(readBackendChoice(null, null, s).choice).toBe(POLARIS_BACKEND);
  });

  it('uses the server value for conversations never picked here, and does not resend it', () => {
    const s = memoryStore();
    expect(readBackendChoice('c-old', 'agent-b', s)).toEqual({ choice: 'agent-b', explicit: false });
    // 升级前的老对话：服务端也没存，显示 Polaris，同样不发
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
    expect(() => writeBackendChoice('c1', 'x', broken)).not.toThrow();
    expect(readBackendChoice('c1', 'agent-b', null).choice).toBe('agent-b');
  });

  it('falls back to Polaris when the picked agent is gone', () => {
    const list = [{ id: 'polaris' }, { id: 'agent-a' }];
    expect(resolveBackend('agent-a', list)).toBe('agent-a');
    expect(resolveBackend('agent-gone', list)).toBe(POLARIS_BACKEND);
  });

  it('labels Polaris as Polaris · Claude and agents by their name', () => {
    expect(backendLabel(undefined)).toBe('Polaris · Claude');
    expect(backendLabel({ kind: 'polaris', name: 'Polaris' })).toBe('Polaris · Claude');
    expect(backendLabel({ kind: 'acp', name: 'Claude Code' })).toBe('Claude Code');
  });

  it('the panel sends only an explicit pick and hides the picker when only Polaris exists', () => {
    expect(panelSource).toContain('backends.length > 1');
    expect(panelSource).toContain('const sendBackend = backendChoice.explicit ? backend : undefined;');
    expect(panelSource).toMatch(/\n\s+backend: sendBackend,\n\s+onMeta:/);
    expect(panelSource).toContain('setServerBackend(row?.backend ?? null)');
    expect(panelSource).toContain('LOCAL_AGENTS_HREF');
    expect(LOCAL_AGENTS_HREF).toBe('/settings?tab=agents');
  });

  it('moves through the picker with the keyboard', () => {
    expect(listboxKey('ArrowDown', -1, 3)).toEqual({ focus: 0 });
    expect(listboxKey('ArrowDown', 2, 3)).toEqual({ focus: 0 });
    expect(listboxKey('ArrowUp', 0, 3)).toEqual({ focus: 2 });
    expect(listboxKey('Home', 1, 3)).toEqual({ focus: 0 });
    expect(listboxKey('End', 0, 3)).toEqual({ focus: 2 });
    expect(listboxKey('Escape', 1, 3)).toBe('close');
    expect(listboxKey('a', 1, 3)).toBeNull();
  });
});

describe('agent errors read as what to do', () => {
  it('turns each agent error code into a plain sentence', () => {
    expect(agentErrorText('ACP_AGENT_NOT_INSTALLED')).toContain('安装');
    expect(agentErrorText('ACP_AGENT_NEEDS_LOGIN')).toContain('终端');
    expect(agentErrorText('ACP_AGENT_CRASHED')).toContain('退出');
    expect(agentErrorText('ACP_AGENT_TIMEOUT')).toContain('回应');
    expect(agentErrorText(undefined, 'Error: ACP_AGENT_NOT_AVAILABLE')).toContain('移除或停用');
    expect(agentErrorText('CHAT_FAILED')).toBeNull();
  });

  it('links to Local agents for the fixable ones and keeps the raw detail', () => {
    const block = errorBlock('Claude Code: Authentication required (code -32000)', true, 'ACP_AGENT_NEEDS_LOGIN');
    expect(block.action).toBe('agent-settings');
    expect(block.detail).toContain('Authentication required');
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <NoticeView block={block} />
      </MemoryRouter>,
    );
    expect(html).toMatch(/<a [^>]*href="\/settings\?tab=agents"/);
    expect(errorBlock('pi: agent exited (code 1)', true, 'ACP_AGENT_CRASHED').action).toBeUndefined();
  });

  it('leaves Polaris’s own errors alone', () => {
    expect(errorBlock('boom').action).toBeUndefined();
    expect(errorBlock('LLM_NOT_CONFIGURED').action).toBe('model-settings');
  });
});
