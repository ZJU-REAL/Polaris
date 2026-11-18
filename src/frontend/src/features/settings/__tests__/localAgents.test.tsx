import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import type { AcpAgentRead, AcpAgentTemplate } from '../../../lib/api';
import { AGENTS_KEY, LocalAgentsSettings } from '../LocalAgentsSettings';
import {
  POLICY_OPTIONS,
  agentFix,
  agentStatus,
  envRowIssue,
  envRowsPayload,
  freeSlug,
  probeCapabilities,
  slugify,
  splitArgs,
  templateDescription,
} from '../localAgentsModel';

/* ============================================================
   设置 → 本机智能体。

   钉住：
   1. 状态只按上一次检查连接说话：没检查过不说「已连接」，没登录、没安装各有一个词；
   2. 出问题时给出该运行的那条命令（安装 / 登录）；
   3. pi 动手前不征求同意，界面上要说清权限设置对它无效；
   4. 「每次询问」是默认、排第一，每个选项都有一句白话说明。
   ============================================================ */

const probe = {
  protocol_version: 1,
  name: 'claude-agent-acp',
  title: 'Claude Code',
  version: '0.108.1',
  load_session: true,
  mcp_http: true,
  mcp_sse: false,
  prompt_image: true,
  auth_methods: [],
};

function agent(over: Partial<AcpAgentRead> = {}): AcpAgentRead {
  return {
    id: 'a1',
    slug: 'claude-code',
    name: 'Claude Code',
    template: 'claude-code',
    command: 'claude-agent-acp',
    args: [],
    permission_policy: 'ask',
    enabled: true,
    last_probe: null,
    last_error: null,
    last_probed_at: null,
    env_keys: [],
    command_found: true,
    error_kind: null,
    permission_prompts: true,
    ...over,
  };
}

function template(over: Partial<AcpAgentTemplate> = {}): AcpAgentTemplate {
  return {
    id: 'claude-code',
    name: 'Claude Code',
    description: 'Anthropic’s coding agent',
    command: 'claude-agent-acp',
    args: [],
    detect: ['claude-agent-acp'],
    install: 'npm install -g @agentclientprotocol/claude-agent-acp',
    login: 'claude auth login',
    homepage: 'https://github.com/agentclientprotocol/claude-agent-acp',
    permission_prompts: true,
    env_passthrough: [],
    installed: true,
    found: {},
    ...over,
  };
}

const pi = template({
  id: 'pi',
  name: 'pi',
  description: 'pi',
  command: 'pi-acp',
  detect: ['pi-acp', 'pi'],
  install: 'npm install -g @earendil-works/pi-coding-agent pi-acp',
  login: 'pi-acp --terminal-login',
  permission_prompts: false,
  installed: false,
});

function render(agents: AcpAgentRead[], templates: AcpAgentTemplate[]): string {
  const client = new QueryClient();
  client.setQueryData(AGENTS_KEY, agents);
  client.setQueryData(['acp-templates'], templates);
  return renderToStaticMarkup(
    <QueryClientProvider client={client}>
      <LocalAgentsSettings />
    </QueryClientProvider>,
  );
}

describe('agent status', () => {
  it('does not claim a connection before a check', () => {
    expect(agentStatus(agent())).toEqual({ tone: 'idle', label: { zh: '未检查', en: 'Not checked' } });
    expect(agentStatus(agent({ last_probe: probe })).label.zh).toBe('已连接');
  });

  it('names each failure', () => {
    expect(agentStatus(agent({ command_found: false })).label.zh).toBe('未安装');
    expect(agentStatus(agent({ last_error: 'x', error_kind: 'needs_login' }))).toEqual({
      tone: 'warn',
      label: { zh: '需要登录', en: 'Needs sign-in' },
    });
    expect(agentStatus(agent({ last_error: 'x', error_kind: 'crashed' })).tone).toBe('err');
    expect(agentStatus(agent({ last_error: 'x', error_kind: 'timeout' })).label.en).toBe('No response');
    expect(agentStatus(agent({ enabled: false, last_probe: probe })).label.zh).toBe('已停用');
  });

  it('offers the command that fixes it', () => {
    const tpl = template();
    expect(agentFix(agent({ command_found: false }), tpl)).toEqual({ kind: 'install', command: tpl.install });
    expect(agentFix(agent({ last_error: 'x', error_kind: 'needs_login' }), tpl)).toEqual({
      kind: 'login',
      command: 'claude auth login',
    });
    expect(agentFix(agent({ last_error: 'x', error_kind: 'crashed' }), tpl)).toBeNull();
    expect(agentFix(agent({ command_found: false }), undefined)).toBeNull();
  });
});

describe('the Local agents tab', () => {
  it('shows an empty list and every template, pi included, with install hints collapsed', () => {
    const html = render([], [template(), pi]);
    expect(html).toContain('本机智能体');
    expect(html).toContain('还没有接入智能体');
    expect(html).toContain('Claude Code');
    expect(html).toContain('pi');
    expect(html).toContain('极简的开源编程智能体');
    // 安装命令默认收起
    expect(html).not.toContain('npm install -g');
    expect(html).toContain('安装方法');
    // pi 动手前不问：说清楚
    expect(html).toContain('权限设置对它不起作用');
    expect(html).toContain('自定义命令');
  });

  it('lists a connected agent with a dot and a word, and no longer offers its template', () => {
    const html = render([agent({ last_probe: probe })], [template(), pi]);
    expect(html).toContain('st-dot-ok');
    expect(html).toContain('已连接');
    expect(html).toContain('Claude Code 0.108.1');
    expect(html).toContain('可查你的文献库');
    expect(html).toContain('检查连接');
    // 已添加的模板不再出现在「添加智能体」里；pi 还在
    expect(html).not.toContain('Anthropic 的编程智能体');
    expect(html).toContain('极简的开源编程智能体');
  });

  it('puts the sign-in command right under an agent that needs it', () => {
    const html = render(
      [agent({ last_error: 'Authentication required', error_kind: 'needs_login' })],
      [template()],
    );
    expect(html).toContain('需要登录');
    expect(html).toContain('claude auth login');
    // 原始错误只在展开后出现
    expect(html).not.toContain('Authentication required');
  });

  it('never shows internal words or an ID field', () => {
    const html = render([agent()], [template(), pi]);
    expect(html).not.toMatch(/后端|backend|ACP backend|slug|标识/);
  });
});

describe('permission choices', () => {
  it('defaults to asking, and every option explains itself', () => {
    expect(POLICY_OPTIONS[0]!.value).toBe('ask');
    expect(POLICY_OPTIONS.map((o) => o.value).sort()).toEqual(['ask', 'auto', 'deny', 'read_only']);
    for (const o of POLICY_OPTIONS) {
      expect(o.hint.zh.length).toBeGreaterThan(0);
      expect(o.hint.en.length).toBeGreaterThan(0);
    }
    expect(POLICY_OPTIONS.find((o) => o.value === 'auto')!.warn).toBe(true);
    expect(POLICY_OPTIONS[0]!.hint.zh).toContain('5 分钟');
  });
});

describe('form helpers', () => {
  it('splits arguments, derives an internal id, and avoids taken ones', () => {
    expect(splitArgs('  --acp   --verbose ')).toEqual(['--acp', '--verbose']);
    expect(slugify('My Agent!')).toBe('my-agent');
    expect(slugify('智能体')).toBe('agent');
    expect(freeSlug('pi', ['pi', 'pi-2'])).toBe('pi-3');
  });

  it('checks environment variable rows', () => {
    const rows = [
      { key: 'OPENAI_API_KEY', value: 'x' },
      { key: '1BAD', value: 'y' },
      { key: '', value: 'dropped' },
    ];
    expect(envRowIssue(rows[0]!, rows)).toBeNull();
    expect(envRowIssue(rows[1]!, rows)).not.toBeNull();
    expect(envRowsPayload(rows)).toEqual({ OPENAI_API_KEY: 'x', '1BAD': 'y' });
  });

  it('describes capabilities in plain words and translates template descriptions', () => {
    expect(probeCapabilities({ ...probe, mcp_http: false, load_session: false, prompt_image: false })).toEqual([]);
    expect(templateDescription({ id: 'unknown', description: 'Some agent' })).toEqual({ zh: 'Some agent', en: 'Some agent' });
  });

  it('copy is called inside components, not at module top level', () => {
    for (const file of ['../LocalAgentsSettings.tsx', '../localAgentsModel.ts']) {
      const src = readFileSync(fileURLToPath(new URL(file, import.meta.url)), 'utf-8');
      // 顶层（行首）不出现 tr( 调用
      expect(src).not.toMatch(/^(export )?const [A-Za-z_]+ = tr\(/m);
    }
  });
});
