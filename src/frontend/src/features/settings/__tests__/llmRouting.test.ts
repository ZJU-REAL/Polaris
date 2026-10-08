import { describe, expect, it } from 'vitest';
import {
  answerStatus,
  buildRoute,
  parseTargetValue,
  resolveTabParam,
  targetValueOf,
  takeoverAgent,
  whoAnswers,
  withDefaultAgent,
  type RouteDraft,
} from '../llmRoutingModel';
import { initialTabOf } from '../SettingsPage';
import { isLlmNotConfigured } from '../../../lib/llmNotConfigured';

/**
 * 模型与智能体（#840）：智能体也是路由目标；没有「默认」时第一个启用的智能体接管
 * 对话类环节；向量嵌入 / 重排序永远不交给智能体。
 */

const providers = [{ id: 'p1', name: 'DeepSeek' }, { id: 'emb', name: 'SiliconFlow' }];
const claude = { id: 'a1', name: 'Claude Code', slug: 'claude', enabled: true };
const codex = { id: 'a2', name: '', slug: 'codex', enabled: true };
const off = { id: 'a0', name: 'Old', slug: 'old', enabled: false };

const route = (stage: string, target: { provider_id?: string; acp_agent_id?: string }, model = '') => ({
  stage,
  provider_id: target.provider_id ?? null,
  acp_agent_id: target.acp_agent_id ?? null,
  model,
});

describe('who answers model calls', () => {
  it('uses the default route when it points at a model API', () => {
    const routes = [route('default', { provider_id: 'p1' }, 'deepseek-chat')];
    expect(whoAnswers('writing', routes, [claude], providers)).toEqual({
      kind: 'provider', providerId: 'p1', name: 'DeepSeek', model: 'deepseek-chat',
    });
    const line = answerStatus(routes, [claude], providers);
    expect(line.tone).toBe('ok');
    expect(line.en).toContain('DeepSeek · deepseek-chat answers by default');
  });

  it('uses the default route when it points at an agent', () => {
    const routes = [route('default', { acp_agent_id: 'a2' })];
    const who = whoAnswers('default', routes, [claude, codex], providers);
    expect(who).toMatchObject({ kind: 'agent', agentId: 'a2', name: 'codex', takeover: false, unavailable: false });
    expect(answerStatus(routes, [claude, codex], providers).en).toContain('The agent "codex" answers by default');
  });

  it('warns when the default agent is gone or turned off', () => {
    const routes = [route('default', { acp_agent_id: 'a0' })];
    expect(whoAnswers('default', routes, [off], providers)).toMatchObject({ unavailable: true });
    expect(answerStatus(routes, [off], providers).tone).toBe('warn');
  });

  it('lets the first enabled agent take over when there is no default route', () => {
    const who = whoAnswers('review', [], [off, claude, codex], providers);
    expect(who).toMatchObject({ kind: 'agent', agentId: 'a1', takeover: true });
    const line = answerStatus([], [off, claude, codex], providers);
    expect(line.zh).toContain('所有对话类环节由「Claude Code」回答');
    // 没有模型 API 的嵌入：说清楚检索退回关键词
    expect(line.en).toContain('falls back to keywords');
    expect(takeoverAgent(true, [claude])).toBeNull();
  });

  it('says nothing is available when there is no default and no enabled agent', () => {
    expect(whoAnswers('default', [], [off], providers)).toEqual({ kind: 'none' });
    const line = answerStatus([], [off], providers);
    expect(line.tone).toBe('warn');
    expect(line.en).toContain('add an agent or a model API');
  });

  it('never hands embedding or rerank to an agent', () => {
    expect(whoAnswers('embedding', [], [claude], providers)).toEqual({ kind: 'none' });
    expect(whoAnswers('rerank', [route('default', { acp_agent_id: 'a1' })], [claude], providers)).toEqual({ kind: 'none' });
    const withEmb = [route('embedding', { provider_id: 'emb' }, 'bge-m3')];
    expect(whoAnswers('embedding', withEmb, [claude], providers)).toMatchObject({ kind: 'provider', name: 'SiliconFlow' });
    // 有嵌入模型时不再提示退回关键词
    expect(answerStatus(withEmb, [claude], providers).en).not.toContain('keywords');
  });
});

const draft = (patch: Partial<RouteDraft>): RouteDraft => ({
  provider_id: '',
  acp_agent_id: '',
  model: '',
  temperature: '',
  effort: '',
  context_window: '',
  budgets: {},
  ...patch,
});

describe('routing row payload', () => {
  it('sends agent rows with a null provider and an optional model', () => {
    expect(buildRoute('default', draft({ acp_agent_id: 'a1', temperature: '0.3', effort: 'high' }), {
      contextWindow: null,
    })).toEqual({ stage: 'default', provider_id: null, acp_agent_id: 'a1', model: '' });
    expect(buildRoute('writing', draft({ acp_agent_id: 'a1', model: ' sonnet ' }), {
      contextWindow: 200000,
      budgets: { context: 9000 },
    })).toEqual({
      stage: 'writing', provider_id: null, acp_agent_id: 'a1', model: 'sonnet',
      context_window: 200000, input_budgets: { context: 9000 },
    });
  });

  it('keeps model API rows as before and drops incomplete ones', () => {
    expect(buildRoute('default', draft({ provider_id: 'p1', model: 'deepseek-chat', temperature: '0.2', effort: 'low' }), {
      contextWindow: null,
    })).toEqual({
      stage: 'default', provider_id: 'p1', acp_agent_id: null, model: 'deepseek-chat', temperature: 0.2, effort: 'low',
    });
    expect(buildRoute('default', draft({ provider_id: 'p1' }), { contextWindow: null })).toBeNull();
    expect(buildRoute('default', draft({}), { contextWindow: null })).toBeNull();
  });

  it('refuses agents on embedding and rerank', () => {
    expect(buildRoute('embedding', draft({ acp_agent_id: 'a1' }), { contextWindow: null })).toBeNull();
    expect(buildRoute('rerank', draft({ acp_agent_id: 'a1' }), { contextWindow: null })).toBeNull();
  });

  it('round-trips the picker value', () => {
    expect(parseTargetValue(targetValueOf({ provider_id: '', acp_agent_id: 'a1' }))).toEqual({ provider_id: '', acp_agent_id: 'a1' });
    expect(parseTargetValue('p1')).toEqual({ provider_id: 'p1', acp_agent_id: '' });
    expect(parseTargetValue('')).toEqual({ provider_id: '', acp_agent_id: '' });
  });

  it('sets the default route to an agent and keeps every other row', () => {
    const routes = [
      { ...route('default', { provider_id: 'p1' }, 'deepseek-chat'), temperature: 0.5, context_window: 64000 },
      route('embedding', { provider_id: 'emb' }, 'bge-m3'),
    ];
    const next = withDefaultAgent(routes, 'a1');
    expect(next).toHaveLength(2);
    expect(next.find((r) => r.stage === 'embedding')).toEqual(routes[1]);
    expect(next.find((r) => r.stage === 'default')).toEqual({
      stage: 'default', provider_id: null, acp_agent_id: 'a1', model: '', context_window: 64000,
    });
    expect(withDefaultAgent([], 'a2')).toEqual([{ stage: 'default', provider_id: null, acp_agent_id: 'a2', model: '' }]);
  });
});

describe('settings deep link', () => {
  it('sends the old ?tab=agents to Models & agents and focuses the agents section', () => {
    expect(resolveTabParam('agents')).toEqual({ tab: 'llm', focus: 'agents' });
    expect(initialTabOf('agents')).toBe('llm');
    expect(initialTabOf('llm')).toBe('llm');
    expect(resolveTabParam('llm').focus).toBeNull();
    expect(initialTabOf(null)).toBe('personal');
    expect(initialTabOf('nonsense')).toBe('personal');
  });
});

describe('no-model error detection', () => {
  it('recognises the error code, the exception name and the backend wording', () => {
    expect(isLlmNotConfigured('LLM_NOT_CONFIGURED')).toBe(true);
    expect(isLlmNotConfigured('Error: LLMNotConfiguredError: ...')).toBe(true);
    expect(isLlmNotConfigured('No model configured — add an agent backend or a model API')).toBe(true);
    expect(isLlmNotConfigured('rate limited')).toBe(false);
  });
});
