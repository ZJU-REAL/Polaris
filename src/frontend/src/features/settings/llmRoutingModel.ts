import type { AcpAgentRead, LlmEffort, LlmRoute } from '../../lib/api';

/* ============================================================
   模型与智能体（#840）的纯逻辑：谁在回答模型调用、路由表行怎么提交。

   智能体（Claude Code、Codex……）和模型 API 一样能当路由目标；没有「默认」路由时，
   第一个启用的智能体接管所有对话类环节。向量嵌入、重排序永远不交给智能体。
   文案只存中英两份，不在这里调 tr。
   ============================================================ */

/** 能力型环节：不跟随「默认」，也不能交给智能体。 */
export const CAPABILITY_STAGES: ReadonlySet<string> = new Set(['embedding', 'rerank']);

export const agentAllowedFor = (stage: string): boolean => !CAPABILITY_STAGES.has(stage);

export const agentName = (a: Pick<AcpAgentRead, 'name' | 'slug'>): string => a.name || a.slug;

/** 路由表下拉框里智能体选项的 value 前缀（与模型 API 的 id 区分开）。 */
const AGENT_PREFIX = 'agent:';

export interface RouteTarget {
  provider_id: string;
  acp_agent_id: string;
}

/** 下拉框当前值：选了智能体是 agent:<id>，否则是 provider id（'' = 未配置）。 */
export function targetValueOf(t: RouteTarget): string {
  return t.acp_agent_id ? `${AGENT_PREFIX}${t.acp_agent_id}` : t.provider_id;
}

export function parseTargetValue(v: string): RouteTarget {
  return v.startsWith(AGENT_PREFIX)
    ? { provider_id: '', acp_agent_id: v.slice(AGENT_PREFIX.length) }
    : { provider_id: v, acp_agent_id: '' };
}

export interface RouteDraft extends RouteTarget {
  model: string;
  temperature: string;
  /** '' = 不发送该参数，用模型默认档位 */
  effort: string;
  /** 模型的上下文窗口（token）；'' = 没填 */
  context_window: string;
  /** 输入预算覆盖（键 → 输入框里的字符串）；缺键或 '' = 用默认值（#811） */
  budgets: Record<string, string>;
}

/** 这一行是否填完整：智能体只要选了就行（model 可空），模型 API 还要有 model。 */
export function draftComplete(d: RouteTarget & { model: string }): boolean {
  if (d.acp_agent_id) return true;
  return !!d.provider_id && !!d.model.trim();
}

/**
 * 一行草稿 → PUT 的一条路由；不完整返回 null（不提交）。
 * 智能体行：provider_id 为 null、model 可为空串，temperature / effort 智能体不认，不发；
 * 窗口与预算照带（材料多长仍要按它们裁）。
 */
export function buildRoute(
  stage: string,
  d: RouteDraft,
  extras: { contextWindow: number | null; budgets?: Record<string, number> | null },
): LlmRoute | null {
  if (!draftComplete(d)) return null;
  // 能力型环节交不了智能体：残留的选择直接丢掉
  if (d.acp_agent_id && !agentAllowedFor(stage)) return null;
  const common = {
    ...(extras.contextWindow !== null ? { context_window: extras.contextWindow } : {}),
    ...(extras.budgets ? { input_budgets: extras.budgets } : {}),
  };
  if (d.acp_agent_id) {
    return { stage, provider_id: null, acp_agent_id: d.acp_agent_id, model: d.model.trim(), ...common };
  }
  const t = d.temperature.trim();
  return {
    stage,
    provider_id: d.provider_id,
    acp_agent_id: null,
    model: d.model.trim(),
    ...(t !== '' && Number.isFinite(Number(t)) ? { temperature: Number(t) } : {}),
    ...(d.effort ? { effort: d.effort as LlmEffort } : {}),
    ...common,
  };
}

/** 把「默认」改成某个智能体，其余行原样保留（PUT 是整表覆盖）。 */
export function withDefaultAgent(routes: readonly LlmRoute[], agentId: string): LlmRoute[] {
  const old = routes.find((r) => r.stage === 'default');
  return [
    ...routes.filter((r) => r.stage !== 'default'),
    {
      stage: 'default',
      provider_id: null,
      acp_agent_id: agentId,
      model: '',
      ...(old?.context_window ? { context_window: old.context_window } : {}),
      ...(old?.input_budgets ? { input_budgets: old.input_budgets } : {}),
    },
  ];
}

/** 没有「默认」路由时接管的智能体：第一个启用的（列表本身按创建时间排）。 */
export function takeoverAgent<A extends Pick<AcpAgentRead, 'enabled'>>(
  hasDefault: boolean,
  agents: readonly A[],
): A | null {
  if (hasDefault) return null;
  return agents.find((a) => a.enabled) ?? null;
}

type AgentLike = Pick<AcpAgentRead, 'id' | 'name' | 'slug' | 'enabled'>;
type ProviderLike = { id: string; name: string };
type RouteLike = Pick<LlmRoute, 'stage' | 'provider_id' | 'acp_agent_id' | 'model'>;

export type Answerer =
  | { kind: 'provider'; providerId: string; name: string; model: string }
  /** takeover = 没有默认路由、由它接管；unavailable = 路由指向的智能体已删除或停用 */
  | { kind: 'agent'; agentId: string; name: string; model: string; takeover: boolean; unavailable: boolean }
  | { kind: 'none' };

function answererOfRoute(r: RouteLike, agents: readonly AgentLike[], providers: readonly ProviderLike[]): Answerer {
  if (r.acp_agent_id) {
    const a = agents.find((x) => x.id === r.acp_agent_id);
    return {
      kind: 'agent',
      agentId: r.acp_agent_id,
      name: a ? agentName(a) : '?',
      model: r.model.trim(),
      takeover: false,
      unavailable: !a || !a.enabled,
    };
  }
  const p = providers.find((x) => x.id === r.provider_id);
  return { kind: 'provider', providerId: r.provider_id ?? '', name: p?.name ?? '?', model: r.model.trim() };
}

/**
 * 某个环节此刻由谁回答：自己的路由 → 默认路由 → 接管的智能体 → 没人。
 * 能力型环节只看自己的路由，不跟随默认、不被接管。
 */
export function whoAnswers(
  stage: string,
  routes: readonly RouteLike[],
  agents: readonly AgentLike[],
  providers: readonly ProviderLike[],
): Answerer {
  const own = routes.find((r) => r.stage === stage);
  if (CAPABILITY_STAGES.has(stage)) {
    return own && own.provider_id ? answererOfRoute(own, agents, providers) : { kind: 'none' };
  }
  if (own) return answererOfRoute(own, agents, providers);
  const def = routes.find((r) => r.stage === 'default');
  if (def) return answererOfRoute(def, agents, providers);
  const t = takeoverAgent(false, agents);
  if (t) {
    return { kind: 'agent', agentId: t.id, name: agentName(t), model: '', takeover: true, unavailable: false };
  }
  return { kind: 'none' };
}

export interface StatusLine {
  tone: 'ok' | 'warn';
  zh: string;
  en: string;
}

/** 页首那句「现在谁在回答模型调用」。 */
export function answerStatus(
  routes: readonly RouteLike[],
  agents: readonly AgentLike[],
  providers: readonly ProviderLike[],
): StatusLine {
  const who = whoAnswers('default', routes, agents, providers);
  const noEmbedding = whoAnswers('embedding', routes, agents, providers).kind === 'none';
  const embedNote = noEmbedding
    ? {
        zh: '未设置向量嵌入，搜索只按关键词匹配。',
        en: 'No embedding model, so search falls back to keywords.',
      }
    : { zh: '', en: '' };
  if (who.kind === 'none') {
    return {
      tone: 'warn',
      zh: '还没有可用的模型，请添加智能体或模型服务。',
      en: 'No model is available yet. Add an agent or a model provider.',
    };
  }
  if (who.kind === 'agent' && who.takeover) {
    return {
      tone: 'ok',
      zh: `由「${who.name}」回答。${embedNote.zh}`,
      en: `“${who.name}” answers. ${embedNote.en}`.trim(),
    };
  }
  if (who.kind === 'agent') {
    const model = who.model ? `（${who.model}）` : '';
    const modelEn = who.model ? ` (${who.model})` : '';
    if (who.unavailable) {
      return {
        tone: 'warn',
        zh: `默认的智能体「${who.name}」${model}已删除或停用，请换一个默认模型。`,
        en: `The default agent “${who.name}”${modelEn} was removed or turned off. Choose another default.`,
      };
    }
    return {
      tone: 'ok',
      zh: `默认由「${who.name}」${model}回答。${embedNote.zh}`,
      en: `“${who.name}”${modelEn} answers by default. ${embedNote.en}`.trim(),
    };
  }
  return {
    tone: 'ok',
    zh: `默认由 ${who.name} · ${who.model} 回答。${embedNote.zh}`,
    en: `${who.name} · ${who.model} answers by default. ${embedNote.en}`.trim(),
  };
}

/**
 * 设置页深链：?tab=agents 是合并前的「智能体后端」，现在落到「模型与智能体」并定位到智能体区。
 */
export function resolveTabParam(param: string | null): { tab: string | null; focus: 'agents' | null } {
  if (param === 'agents') return { tab: 'llm', focus: 'agents' };
  return { tab: param, focus: null };
}

/** 服务端的一行路由 → 路由表里可编辑的草稿。 */
export function routeToDraft(r: LlmRoute): RouteDraft {
  return {
    provider_id: r.provider_id ?? '',
    acp_agent_id: r.acp_agent_id ?? '',
    model: r.model,
    temperature: r.temperature === null || r.temperature === undefined ? '' : String(r.temperature),
    effort: r.effort ?? '',
    context_window: r.context_window ? String(r.context_window) : '',
    budgets: Object.fromEntries(Object.entries(r.input_budgets ?? {}).map(([k, v]) => [k, String(v)])),
  };
}

export function draftsFromRoutes(routes: readonly LlmRoute[]): Record<string, RouteDraft> {
  return Object.fromEntries(routes.map((r) => [r.stage, routeToDraft(r)]));
}

function sameDraft(a: RouteDraft | undefined, b: RouteDraft | undefined): boolean {
  if (!a || !b) return a === b;
  const budgetKeys = new Set([...Object.keys(a.budgets), ...Object.keys(b.budgets)]);
  return (
    a.provider_id === b.provider_id &&
    a.acp_agent_id === b.acp_agent_id &&
    a.model === b.model &&
    a.temperature === b.temperature &&
    a.effort === b.effort &&
    a.context_window === b.context_window &&
    [...budgetKeys].every((k) => (a.budgets[k] ?? '') === (b.budgets[k] ?? ''))
  );
}

/**
 * 服务端路由变了（别处「设为默认模型」、删了智能体……）时把新表合进草稿：
 * 用户没动过的行跟着服务端走，动过（含清掉）的行保留他的改动，等他自己保存。
 * 以前是整表重置，没保存的编辑一声不响就没了。
 */
export function rebaseDrafts(
  base: Readonly<Record<string, RouteDraft>>,
  rows: Readonly<Record<string, RouteDraft>>,
  server: Readonly<Record<string, RouteDraft>>,
): Record<string, RouteDraft> {
  const out: Record<string, RouteDraft> = {};
  for (const stage of new Set([...Object.keys(base), ...Object.keys(rows), ...Object.keys(server)])) {
    const edited = !sameDraft(rows[stage], base[stage]);
    const pick = edited ? rows[stage] : server[stage];
    if (pick) out[stage] = pick;
  }
  return out;
}

/** 草稿和服务端那份比，有没有没保存的改动。 */
export function draftsDirty(
  base: Readonly<Record<string, RouteDraft>>,
  rows: Readonly<Record<string, RouteDraft>>,
): boolean {
  const stages = new Set([...Object.keys(base), ...Object.keys(rows)]);
  return [...stages].some((s) => !sameDraft(rows[s], base[s]));
}
