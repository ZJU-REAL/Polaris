import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { AGENTS_KEY, AcpAgentsSettings, invalidateAfterAgentChange } from '../AcpAgentsSettings';
import {
  DEFAULT_POLICY,
  POLICY_OPTIONS,
  acpErrorText,
  envRowIssue,
  envRowsPayload,
  freeSlug,
  joinCommand,
  probeCapabilities,
  probeTitle,
  slugify,
  splitArgs,
} from '../acpAgentsModel';

const probe = {
  protocol_version: 1,
  name: 'claude-code',
  title: 'Claude Code',
  version: '1.2.0',
  load_session: true,
  mcp_http: true,
  mcp_sse: false,
  prompt_image: false,
  auth_methods: [],
};

describe('agent backends model', () => {
  it('splits arguments on whitespace and joins them back for display', () => {
    expect(splitArgs('  --acp   --verbose ')).toEqual(['--acp', '--verbose']);
    expect(splitArgs('')).toEqual([]);
    expect(joinCommand('gemini', ['--acp'])).toBe('gemini --acp');
    expect(joinCommand('codex-acp', null)).toBe('codex-acp');
  });

  it('turns env rows into a payload, dropping rows without a name', () => {
    expect(
      envRowsPayload([
        { key: ' API_KEY ', value: 'secret' },
        { key: '', value: 'ignored' },
        { key: 'EMPTY', value: '' },
      ]),
    ).toEqual({ API_KEY: 'secret', EMPTY: '' });
  });

  it('flags bad and duplicate env names but not empty rows', () => {
    const rows = [
      { key: '1BAD', value: '' },
      { key: 'DUP', value: 'a' },
      { key: 'DUP', value: 'b' },
      { key: '', value: 'x' },
    ];
    expect(envRowIssue(rows[0]!, rows)?.en).toMatch(/Letters/);
    expect(envRowIssue(rows[1]!, rows)?.en).toBe('Duplicate name');
    expect(envRowIssue(rows[3]!, rows)).toBeNull();
  });

  it('de-duplicates slugs with -2, -3 suffixes', () => {
    expect(freeSlug('codex', [])).toBe('codex');
    expect(freeSlug('codex', ['codex'])).toBe('codex-2');
    expect(freeSlug('codex', ['codex', 'codex-2'])).toBe('codex-3');
  });

  it('builds a valid slug from a display name', () => {
    expect(slugify('My Agent!')).toBe('my-agent');
    expect(slugify('智能体')).toBe('agent');
  });

  it('describes probe results in plain words', () => {
    expect(probeTitle(probe)).toBe('Claude Code 1.2.0');
    expect(probeCapabilities(probe).map((c) => c.en)).toEqual([
      'Can resume earlier sessions',
      'Can use Polaris tools (MCP)',
    ]);
    expect(probeCapabilities({ ...probe, load_session: false, mcp_http: false })).toEqual([]);
  });

  it('only the automatic policy carries a warning', () => {
    expect(POLICY_OPTIONS.map((o) => o.value)).toEqual(['deny', 'ask', 'read_only', 'auto']);
    expect(POLICY_OPTIONS.filter((o) => o.warn).map((o) => o.value)).toEqual(['auto']);
  });

  it('offers "ask me each time" and makes it the default for custom agents', () => {
    const ask = POLICY_OPTIONS.find((o) => o.value === 'ask');
    expect(ask?.label).toEqual({ zh: '每次都问我', en: 'Ask me each time' });
    expect(ask?.hint?.en).toMatch(/refuses if nobody answers/);
    expect(DEFAULT_POLICY).toBe('ask');
    const src = readFileSync(join(__dirname, '..', 'AcpAgentsSettings.tsx'), 'utf8');
    expect(src).toContain('useState<AcpPermissionPolicy>(DEFAULT_POLICY)');
    // 模板一键添加不带策略，由后端给默认值（ask）
    expect(src).toContain('createDeduped({ slug: t.id, template: t.id }, taken)');
  });

  it('maps backend error codes and leaves unknown ones alone', () => {
    expect(acpErrorText('SLUG_TAKEN')?.en).toMatch(/taken/);
    expect(acpErrorText('BAD_ENV_KEY: 1X')?.en).toMatch(/invalid/);
    expect(acpErrorText('something else')).toBeNull();
  });
});

describe('agent backends settings tab', () => {
  it('renders the intro and the add section before data arrives', () => {
    const html = renderToStaticMarkup(
      <QueryClientProvider client={new QueryClient()}>
        <AcpAgentsSettings />
      </QueryClientProvider>,
    );
    expect(html).toContain('智能体后端');
    expect(html).toContain('Agent Client Protocol');
    expect(html).toContain('添加智能体');
    expect(html).toContain('自定义智能体');
  });

  it('lives first inside the Models & agents tab, with no tab of its own (#840)', () => {
    const page = readFileSync(join(__dirname, '..', 'SettingsPage.tsx'), 'utf8');
    expect(page).not.toMatch(/\| 'agents'/);
    expect(page).not.toMatch(/ALL_TABS: Tab\[\] = \[[^\]]*'agents'/);
    const workspace = page.slice(page.indexOf('const workspaceItems'));
    expect(workspace.slice(0, 800)).not.toContain("v: 'agents'");
    expect(workspace.slice(0, 800)).toContain("tr('模型与智能体', 'Models & agents')");
    const llmTab = page.slice(page.indexOf('export function LlmTab('));
    const agents = llmTab.indexOf('<AcpAgentsSettings />');
    expect(agents).toBeGreaterThan(-1);
    expect(agents).toBeLessThan(llmTab.indexOf('<ProvidersSection />'));
    expect(llmTab.indexOf('<AnswerStatusHeader />')).toBeLessThan(agents);
  });

  it('never calls tr() at module level', () => {
    const src = readFileSync(join(__dirname, '..', 'acpAgentsModel.ts'), 'utf8');
    // 文案表只存中英两份：这个文件根本不该引入 tr
    expect(src).not.toMatch(/import\s*\{[^}]*\btr\b[^}]*\}\s*from/);
  });
});

describe('agent changes refresh the routing table (#850)', () => {
  it('deleting or toggling an agent invalidates the model routes', () => {
    const qc = new QueryClient();
    qc.setQueryData(['llm', 'routes'], []);
    qc.setQueryData(['llm', 'providers'], []);
    qc.setQueryData(AGENTS_KEY, []);
    qc.setQueryData(['chat-backends'], []);
    invalidateAfterAgentChange(qc);
    expect(qc.getQueryState(['llm', 'routes'])?.isInvalidated).toBe(true);
    expect(qc.getQueryState(AGENTS_KEY)?.isInvalidated).toBe(true);
    expect(qc.getQueryState(['chat-backends'])?.isInvalidated).toBe(true);
  });

  it('every agent mutation goes through it', () => {
    const src = readFileSync(join(__dirname, '..', 'AcpAgentsSettings.tsx'), 'utf8');
    const row = src.slice(src.indexOf('function AgentRow('), src.indexOf('const policy = POLICY_OPTIONS'));
    // update（启停）、probe、remove 都走 refresh，refresh 走 invalidateAfterAgentChange
    expect(row).toMatch(/const refresh = [\s\S]*invalidateAfterAgentChange\(queryClient\)/);
    expect(row).toMatch(/const remove = useMutation\(\{[\s\S]*refresh\(\);/);
    expect(row).toMatch(/const update = useMutation\(\{[\s\S]*onSuccess: \(row\) => refresh\(row\)/);
  });

  it('the old ?tab=agents deep link only scrolls on landing', () => {
    const page = readFileSync(join(__dirname, '..', 'SettingsPage.tsx'), 'utf8');
    const setTab = page.slice(page.indexOf('const setTab = (next: Tab) => {'));
    expect(setTab.slice(0, 300)).toContain('setFocusAgents(false)');
  });
});
