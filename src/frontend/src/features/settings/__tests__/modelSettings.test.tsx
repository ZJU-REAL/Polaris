import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import type { LlmCheckResult, LlmStatus } from '../../../lib/api';
import {
  LLM_STATUS_KEY,
  ModelSettings,
  checkResultText,
  connectionState,
  keySetupText,
  withCode,
} from '../ModelSettings';
import { ALL_TABS, LEGACY_TABS, initialTabOf } from '../SettingsPage';

/* ============================================================
   设置 → 模型：只读的一块。

   钉三件事：
   1. 没检查过就不说「已连接」——引擎拿到了 key 不等于 key 是对的；
   2. 每个错误码各有一句能照着做的话；
   3. 撤掉的「模型与智能体」「智能体后端」「我的模型」旧深链都落到这里。
   ============================================================ */

const status: LlmStatus = {
  configured: true,
  model: 'claude-opus-5',
  base_host: 'api.anthropic.com',
  embedding: { model: 'embed-x', available: true },
  rerank: { model: 'rerank-x', available: false },
};

const failed = (error: LlmCheckResult['error'], detail: string | null = null): LlmCheckResult => ({
  ok: false,
  latency_ms: null,
  error,
  detail,
});

function render(data: LlmStatus, embeddingSpace?: { mismatched: boolean }): string {
  const client = new QueryClient();
  client.setQueryData(LLM_STATUS_KEY, data);
  if (embeddingSpace) {
    client.setQueryData(['admin', 'embedding-space'], {
      active: null,
      routed_model: 'embed-x',
      spaces: [],
      ...embeddingSpace,
    });
  }
  return renderToStaticMarkup(
    <QueryClientProvider client={client}>
      <ModelSettings />
    </QueryClientProvider>,
  );
}

describe('connection state', () => {
  it('does not claim a connection before a check', () => {
    expect(connectionState(status, null).label).toBe('已设置 API key');
    expect(connectionState(status, null).tone).toBe('idle');
    expect(connectionState(status, null, true).tone).toBe('busy');
  });

  it('says connected only after a passing check', () => {
    expect(connectionState(status, { ok: true, latency_ms: 420, error: null, detail: null })).toEqual({
      tone: 'ok',
      label: '已连接',
    });
    expect(connectionState(status, failed('auth_failed'))).toEqual({ tone: 'err', label: '未连接' });
  });

  it('a missing key wins over everything else', () => {
    const missing = { configured: false };
    expect(connectionState(missing, null).label).toBe('缺少 API key');
    expect(connectionState(missing, { ok: true, latency_ms: 1, error: null, detail: null }).label).toBe('缺少 API key');
  });
});

describe('check result text', () => {
  it('reports latency on success', () => {
    expect(checkResultText({ ok: true, latency_ms: 420.4, error: null, detail: null }, status)).toBe('连接正常，耗时 420 ms');
    expect(checkResultText({ ok: true, latency_ms: null, error: null, detail: null }, status)).toBe('连接正常');
  });

  it('has a distinct sentence per error code', () => {
    const codes = ['not_configured', 'auth_failed', 'unreachable', 'model_not_found', 'rate_limited', 'failed'] as const;
    const texts = codes.map((code) => checkResultText(failed(code), status));
    expect(new Set(texts).size).toBe(codes.length);
    expect(checkResultText(failed('unreachable'), status)).toContain('api.anthropic.com');
    expect(checkResultText(failed('model_not_found'), status)).toContain('claude-opus-5');
    expect(checkResultText(failed('auth_failed'), status)).toContain('被拒绝');
  });

  it('still reads as a sentence when the host is unknown', () => {
    expect(checkResultText(failed('unreachable'), { ...status, base_host: '' })).toBe('无法连接 模型服务，请检查网络。');
  });
});

describe('model settings block', () => {
  it('shows the fixed model and both search lines', () => {
    const html = render(status);
    expect(html).toContain('claude-opus-5');
    expect(html).toContain('api.anthropic.com');
    expect(html).toContain('检查连接');
    expect(html).toContain('已设置 API key');
    expect(html).not.toContain('已连接');
    expect(html).toContain('embed-x');
    expect(html).toContain('搜索结果不再重排');
    // 模型不可选：这一块没有任何输入控件
    expect(html).not.toMatch(/<input|<select|<textarea/);
  });

  it('explains how to provide the key when there is none', () => {
    const html = render({ ...status, configured: false });
    expect(html).toContain('缺少 API key');
    for (const name of ['ANTHROPIC_API_KEY', 'LITELLM_API_KEY', 'LITELLM_API_BASE']) {
      expect(html).toMatch(new RegExp(`<code[^>]*>${name}</code>`));
    }
    expect(html).not.toContain('`');
    expect(html).not.toContain('已设置 API key');
    // 没有 key 时查也是白查
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>检查连接<\/button>/);
  });

  it('says keyword search takes over when semantic search is unavailable', () => {
    const html = render({ ...status, embedding: { model: 'embed-x', available: false } });
    expect(html).toContain('搜索改用关键词匹配');
  });

  it('offers the index rebuild only when the index came from another model', () => {
    expect(render(status)).not.toContain('重建索引');
    expect(render(status, { mismatched: false })).not.toContain('重建索引');
    const html = render(status, { mismatched: true });
    expect(html).toContain('重建索引');
    // 模型是固定的：不把它说成用户在「换模型」
    expect(html).not.toMatch(/改用|切换|更换/);
  });

  it('renders env var names as code and leaves plain text alone', () => {
    expect(renderToStaticMarkup(<>{withCode('a `B_C` d')}</>)).toMatch(/^a <code[^>]*>B_C<\/code> d$/);
    expect(renderToStaticMarkup(<>{withCode('plain')}</>)).toBe('plain');
    expect(keySetupText()).toContain('`ANTHROPIC_API_KEY`');
  });
});

describe('deep links', () => {
  it('lands the removed tabs on the model block', () => {
    for (const old of ['llm', 'mymodels']) {
      expect(initialTabOf(old)).toBe('model');
      expect(LEGACY_TABS[old]).toBe('model');
    }
    expect(initialTabOf('model')).toBe('model');
  });

  it('opens the local agents tab from ?tab=agents again', () => {
    expect(initialTabOf('agents')).toBe('agents');
    expect(LEGACY_TABS).not.toHaveProperty('agents');
  });

  it('no removed tab is still a real tab', () => {
    for (const old of Object.keys(LEGACY_TABS)) expect(ALL_TABS).not.toContain(old);
    for (const target of Object.values(LEGACY_TABS)) expect(ALL_TABS).toContain(target);
  });

  it('unknown and inherited names fall back to the profile tab', () => {
    expect(initialTabOf(null)).toBe('personal');
    expect(initialTabOf('nope')).toBe('personal');
    expect(initialTabOf('constructor')).toBe('personal');
  });

  it('rewrites the address so the old name does not stick', () => {
    const page = readFileSync(fileURLToPath(new URL('../SettingsPage.tsx', import.meta.url)), 'utf-8');
    expect(page).toContain('<Navigate to={`/settings?tab=${legacyTarget}`} replace />');
    expect(page).toContain("effectiveTab === 'model' && <ModelSettings />");
  });
});
