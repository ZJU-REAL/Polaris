import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { AcpAgentsSettings } from '../AcpAgentsSettings';
import {
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
    expect(POLICY_OPTIONS.map((o) => o.value)).toEqual(['deny', 'read_only', 'auto']);
    expect(POLICY_OPTIONS.filter((o) => o.warn).map((o) => o.value)).toEqual(['auto']);
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

  it('is reachable from the settings page as ?tab=agents in the workspace group', () => {
    const page = readFileSync(join(__dirname, '..', 'SettingsPage.tsx'), 'utf8');
    expect(page).toMatch(/\| 'agents'/);
    expect(page).toMatch(/ALL_TABS: Tab\[\] = \[[^\]]*'agents'/);
    const workspace = page.slice(page.indexOf('const workspaceItems'));
    expect(workspace.slice(0, 800)).toContain("v: 'agents'");
    expect(page).toContain("effectiveTab === 'agents' && <AcpAgentsSettings />");
  });

  it('never calls tr() at module level', () => {
    const src = readFileSync(join(__dirname, '..', 'acpAgentsModel.ts'), 'utf8');
    // 文案表只存中英两份：这个文件根本不该引入 tr
    expect(src).not.toMatch(/import\s*\{[^}]*\btr\b[^}]*\}\s*from/);
  });
});
