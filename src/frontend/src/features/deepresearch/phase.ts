import { errorText } from '../../lib/errors';
import { fmtTokens, parseApiDate } from '../../lib/format';
import { tr } from '../../lib/i18n';
import type {
  ResearchAttention,
  ResearchEstimate,
  ResearchPhase,
  ResearchRunRead,
  ResearchStatus,
  ResearchUsage,
  SurveyProgress,
  SurveyQuery,
  SurveySource,
} from '../../lib/researchApi';

/* ============================================================
   一次研究的 (phase, status, attention) → 界面上的说法与动作。纯函数，页面只管画。

   页面把一次研究画成四段：范围 / 文献 / 想法 / 下一步。后端的 phase 只有
   plan / survey / hypotheses / choose，「下一步」不是一个阶段，而是选定想法之后
   出现的出口。子任务自己的状态（survey.state 等）不参与判断：阶段走到哪、
   卡没卡住，只看 run 这一行，这样重启、重连后画出来的东西不会变。

   所有文案都在函数里 tr()，不在模块顶层求值（切语言要能跟着变）。
   ============================================================ */

export type SectionKey = 'scope' | 'papers' | 'ideas' | 'next';

export const SECTION_ORDER: readonly SectionKey[] = ['scope', 'papers', 'ideas', 'next'];

/**
 * 一段的状态：
 * pending 还没轮到 · running 正在做 · retrying 遇到临时问题、排了自动重试 ·
 * waiting 等用户（回答提问 / 选定想法）· attention 卡住了、要用户处理 ·
 * done 做完了 · stopped 用户停在这一段 · ready（只用于「下一步」）可以往下走了
 */
export type SectionState = 'pending' | 'running' | 'retrying' | 'waiting' | 'attention' | 'done' | 'stopped' | 'ready';

export type Tone = 'ok' | 'warn' | 'err' | 'idle' | 'active';

const PHASE_SECTION: Record<ResearchPhase, SectionKey> = {
  plan: 'scope',
  survey: 'papers',
  hypotheses: 'ideas',
  choose: 'ideas',
};

/** 认不出的 phase（后端以后加了新阶段）按「范围」处理，不崩。 */
function sectionOfPhase(phase: string): SectionKey {
  return PHASE_SECTION[phase as ResearchPhase] ?? 'scope';
}

/** 卡住的原因归到哪一段显示。认不出的原因跟着当前阶段走。 */
export function attentionSection(code: string, phase: string): SectionKey {
  switch (code) {
    case 'PLAN_FAILED':
      return 'scope';
    case 'SURVEY_SOURCES_UNREACHABLE':
    case 'SURVEY_NO_RESULTS':
    case 'SURVEY_TOO_FEW_RELEVANT':
    case 'SURVEY_FAILED':
    case 'LIBRARY_TOO_SMALL':
      return 'papers';
    case 'HYPOTHESES_FAILED':
    case 'NO_HYPOTHESES_SURVIVED':
      return 'ideas';
    default:
      // LLM_UNREACHABLE / LLM_AUTH 哪一步都可能遇到
      return sectionOfPhase(phase);
  }
}

/**
 * 卡住时给用户的动作：
 * retry 原样重试 · edit_scope 打开范围编辑（改关键词）后重新检索 · broaden 放宽范围再检索 ·
 * add_papers 去文献库添加论文 · settings 去设置里看模型
 */
export type AttentionAction = 'retry' | 'edit_scope' | 'broaden' | 'add_papers' | 'settings';

const KNOWN_ACTIONS: readonly AttentionAction[] = ['retry', 'edit_scope', 'broaden', 'add_papers', 'settings'];

/** 引擎没给 action（或给了认不出的）时，按原因猜一个。 */
function fallbackAction(code: string): AttentionAction {
  switch (code) {
    case 'SURVEY_NO_RESULTS':
      return 'edit_scope';
    case 'SURVEY_TOO_FEW_RELEVANT':
      return 'broaden';
    case 'NO_HYPOTHESES_SURVIVED':
      return 'add_papers';
    default:
      return 'retry';
  }
}

/**
 * 每个原因只有一个主动作，个别原因多给一个次要动作（不抢主按钮）。
 * 主动作听引擎的（attention.action）；只有一处例外：密钥被拒时重试没有用，
 * 主动作是去设置里看——这是界面才知道的去处，引擎的 action 里没有这一项。
 */
export function attentionActions(
  attention: Pick<ResearchAttention, 'code' | 'action'>,
): { primary: AttentionAction; secondary?: AttentionAction } {
  const { code } = attention;
  if (code === 'LLM_AUTH') return { primary: 'settings', secondary: 'retry' };
  const given = attention.action as AttentionAction | null | undefined;
  const primary = given && KNOWN_ACTIONS.includes(given) ? given : fallbackAction(code);
  let secondary: AttentionAction | undefined;
  if (code === 'SURVEY_TOO_FEW_RELEVANT' || code === 'LIBRARY_TOO_SMALL') secondary = 'add_papers';
  else if (code === 'NO_HYPOTHESES_SURVIVED' || code === 'SURVEY_NO_RESULTS') secondary = 'retry';
  return secondary && secondary !== primary ? { primary, secondary } : { primary };
}

export function attentionActionLabel(action: AttentionAction): string {
  switch (action) {
    case 'retry':
      return tr('重试', 'Try again');
    case 'edit_scope':
      return tr('调整关键词', 'Adjust keywords');
    case 'broaden':
      return tr('放宽范围再检索', 'Broaden and search again');
    case 'add_papers':
      return tr('添加论文', 'Add papers');
    case 'settings':
      return tr('打开设置', 'Open Settings');
  }
}

/**
 * 卡住的原因 → 一句「什么没成功 + 怎么办」。带数字的原因把数字写进去。
 * kept：文献库里留下了多少篇（survey.progress.kept）。「论文太少」有两种情况——真的少，
 * 或者留下的够了但后面的处理没做完——页面上列着 8 篇却说「只有 0 篇」就是在说谎。
 */
export function attentionText(attention: ResearchAttention | null | undefined, kept?: number): string {
  if (!attention) return '';
  const c = attention.counts ?? {};
  if (attention.code === 'LIBRARY_TOO_SMALL' && typeof c.papers === 'number' && typeof kept === 'number') {
    const need = typeof c.required === 'number' ? c.required : 5;
    if (kept >= need && c.papers < kept) {
      return tr(
        `留下的 ${kept} 篇论文里只有 ${c.papers} 篇处理完了，至少需要 ${need} 篇，请重试`,
        `Only ${c.papers} of the ${kept} kept papers finished processing. At least ${need} are needed. Try again.`,
      );
    }
  }
  if (attention.code === 'SURVEY_TOO_FEW_RELEVANT' && typeof c.found === 'number' && typeof c.relevant === 'number') {
    return tr(
      `找到 ${c.found} 篇，只有 ${c.relevant} 篇相关，请放宽范围`,
      `${c.found} found, only ${c.relevant} relevant. Broaden the scope.`,
    );
  }
  if (attention.code === 'LIBRARY_TOO_SMALL' && typeof c.papers === 'number') {
    const need = typeof c.required === 'number' ? c.required : 5;
    return tr(
      `文献库里只有 ${c.papers} 篇论文，至少需要 ${need} 篇`,
      `The library has ${c.papers} ${c.papers === 1 ? 'paper' : 'papers'}. It needs at least ${need}.`,
    );
  }
  return errorText(attention.code);
}

export interface RunLike {
  phase: ResearchPhase | string;
  status: ResearchStatus | string;
  attention?: ResearchAttention | null;
  next_retry_at?: string | null;
}

/** 四段各自的状态。 */
export function sectionStates(run: RunLike): Record<SectionKey, SectionState> {
  const out: Record<SectionKey, SectionState> = { scope: 'pending', papers: 'pending', ideas: 'pending', next: 'pending' };
  const fill = (current: SectionKey, state: SectionState) => {
    const at = SECTION_ORDER.indexOf(current);
    SECTION_ORDER.forEach((k, i) => {
      if (i < at) out[k] = 'done';
      else if (i === at) out[k] = state;
    });
  };
  const current = sectionOfPhase(run.phase);
  switch (run.status) {
    case 'done':
      fill('next', 'ready');
      break;
    case 'cancelled':
      fill(current, 'stopped');
      break;
    case 'needs_attention':
      fill(run.attention ? attentionSection(run.attention.code, run.phase) : current, 'attention');
      break;
    case 'waiting_user':
      fill(current, 'waiting');
      break;
    default:
      // running，以及认不出的状态：当成还在跑，至少不会把页面画成「做完了」
      fill(current, run.next_retry_at ? 'retrying' : 'running');
  }
  return out;
}

/** 用户现在要做的事（没有就是 null）：回答提问 / 选定想法 / 处理卡住的一步。 */
export type WaitingFor = 'answer' | 'pick' | 'attention' | null;

export function waitingFor(run: RunLike): WaitingFor {
  if (run.status === 'needs_attention') return 'attention';
  if (run.status === 'waiting_user') return run.phase === 'choose' ? 'pick' : 'answer';
  return null;
}

/** 正在进行的阶段的说法（列表行、页头用）。 */
export function phaseLabel(phase: string): string {
  switch (phase) {
    case 'plan':
      return tr('确定范围', 'Scoping');
    case 'survey':
      return tr('检索文献', 'Finding papers');
    case 'hypotheses':
      return tr('寻找假设', 'Looking for hypotheses');
    case 'choose':
      return tr('等你选定想法', 'Waiting for your picks');
    default:
      return tr('运行中', 'Running');
  }
}

/** 一次研究现在的一句话状态：点的颜色 + 词。列表行与页头共用。 */
export function runHeadline(run: RunLike): { tone: Tone; label: string } {
  switch (run.status) {
    case 'done':
      return { tone: 'ok', label: tr('已完成', 'Done') };
    case 'cancelled':
      return { tone: 'idle', label: tr('已停止', 'Stopped') };
    case 'needs_attention':
      return { tone: 'warn', label: tr('需要处理', 'Needs attention') };
    case 'waiting_user':
      return run.phase === 'choose'
        ? { tone: 'warn', label: tr('等你选定想法', 'Waiting for your picks') }
        : { tone: 'warn', label: tr('等你回答', 'Waiting for you') };
    default:
      return { tone: 'active', label: phaseLabel(run.phase) };
  }
}

/** 列表行的第二句：卡住时是原因，其余为空（状态词已经说清楚了）。 */
export function runReason(run: ResearchRunRead): string {
  return run.status === 'needs_attention' ? attentionText(run.attention, run.counts.papers) : '';
}

/** 每一段标题旁的状态词；pending 不写字（留白比「未开始」安静），done 也不写（轨道上已经打了勾）。 */
export function sectionStateLabel(state: SectionState): { tone: Tone; label: string } | null {
  switch (state) {
    case 'running':
      return { tone: 'active', label: tr('进行中', 'In progress') };
    case 'retrying':
      return { tone: 'warn', label: tr('等待重试', 'Waiting to retry') };
    case 'waiting':
      return { tone: 'warn', label: tr('等你处理', 'Waiting for you') };
    case 'attention':
      return { tone: 'warn', label: tr('需要处理', 'Needs attention') };
    case 'stopped':
      return { tone: 'idle', label: tr('已停止', 'Stopped') };
    default:
      return null;
  }
}

/** 轮询间隔：运行中 5 秒，等用户 30 秒，其余不轮询。 */
export function pollInterval(status: string | undefined): number | false {
  if (status === 'running') return 5000;
  if (status === 'waiting_user') return 30000;
  return false;
}

/**
 * 文献检索走到哪个小步：检索 → 筛选 → 沿引用扩展 → 读全文 → 写解读 → 整理。
 * 引擎说了正在做哪一步（step_action）就听它的；没说（任务还没起来、已经结束）时
 * 只看库里的数字，刷新、重启后一致。
 */
export type SurveyStage = 'search' | 'screen' | 'expand' | 'fulltext' | 'summarise' | 'index';

export const SURVEY_STAGES: readonly SurveyStage[] = ['search', 'screen', 'expand', 'fulltext', 'summarise', 'index'];

const ACTION_STAGE: Record<string, SurveyStage> = {
  'wiki.search_candidates': 'search',
  'wiki.snowball': 'search',
  'wiki.score_relevance': 'screen',
  'wiki.expand_citations': 'expand',
  'wiki.fetch_extract': 'fulltext',
  'wiki.compile': 'summarise',
  'wiki.link_concepts': 'index',
  'wiki.daily_digest': 'index',
  'wiki.trend_synthesize': 'index',
  'wiki.update_watermark': 'index',
};

export function surveyStage(p: SurveyProgress, stepAction?: string | null, taskState?: string | null): SurveyStage {
  const known = stepAction ? ACTION_STAGE[stepAction] : undefined;
  if (known) return known;
  // 任务刚建好、还在排步骤：一定是从检索开始（重新检索时库里已有上一次的数字，不能按数字猜）
  if (taskState === 'planning') return 'search';
  if (p.found === 0) return 'search';
  if (p.summarised > 0) return 'summarise';
  if (p.screened >= p.found && p.to_summarise > 0) return 'fulltext';
  if (p.screened > 0) return 'screen';
  return 'search';
}

export function surveyStageLabel(stage: SurveyStage): string {
  switch (stage) {
    case 'search':
      return tr('检索', 'Search');
    case 'screen':
      return tr('筛选', 'Screen');
    case 'expand':
      return tr('沿引用扩展', 'Citations');
    case 'fulltext':
      return tr('读全文', 'Full text');
    case 'summarise':
      return tr('写解读', 'Summarise');
    case 'index':
      return tr('整理', 'Organise');
  }
}

/** 这一小步正在做什么，一句人话（不是日志行）。sources 是范围里选的来源的名字。 */
export function surveyStageCaption(stage: SurveyStage, p: SurveyProgress, sources: string[]): string {
  switch (stage) {
    case 'search': {
      const where = sources.join(tr('、', ', '));
      return where ? tr(`正在 ${where} 检索论文`, `Searching ${where}`) : tr('正在检索论文', 'Searching for papers');
    }
    case 'screen':
      return tr('正在逐篇判断是否和你的方向相关', 'Checking each paper against your direction');
    case 'expand':
      return tr(
        '正在看最相关的几篇引用了谁、被谁引用，把相关的也收进来',
        'Following what the most relevant papers cite and what cites them, and keeping the relevant ones',
      );
    case 'fulltext':
      return tr('正在获取留下论文的全文，拿不到的改用摘要', 'Getting the full text of the kept papers. Abstracts are used where it isn’t available.');
    case 'summarise':
      return p.to_summarise > 0
        ? tr(`正在为最相关的 ${p.to_summarise} 篇写解读`, `Summarising the ${p.to_summarise} most relevant papers`)
        : tr('正在写解读', 'Writing summaries');
    case 'index':
      return tr('正在整理概念并建立索引', 'Linking concepts and building the index');
  }
}

/** 当前小步的完成比例（0..1）；没有总数的小步返回 null（画不定进度）。 */
export function surveyFraction(p: SurveyProgress, stage: SurveyStage = surveyStage(p)): number | null {
  if (stage === 'screen') return p.found > 0 ? Math.min(1, p.screened / p.found) : null;
  if (stage === 'summarise') return p.to_summarise > 0 ? Math.min(1, p.summarised / p.to_summarise) : null;
  return null;
}

/** 各来源一共返回了多少条（去重前）。旧引擎不带这个数：返回 0，句子里就不写这一段。 */
function fetchedOf(p: SurveyProgress): number {
  return typeof p.fetched === 'number' && p.fetched > p.found ? p.fetched : 0;
}

/**
 * 进行中的数字，按先后：「检索到 354 条 · 已筛选 60/85 篇 · 留下 19 篇 · 已写解读 3/20」；
 * 还没有的数字不写。检索到的是各来源返回的条数（有重复），筛选的是去重后取进来的论文。
 */
export function surveyProgressText(p: SurveyProgress): string {
  const parts: string[] = [];
  const fetched = fetchedOf(p);
  if (fetched > 0) parts.push(tr(`检索到 ${fetched} 条`, `${fetched} results`));
  else if (p.found > 0) parts.push(tr(`找到 ${p.found} 篇`, `${p.found} found`));
  if (p.screened > 0) {
    parts.push(
      p.screened < p.found
        ? tr(`已筛选 ${p.screened}/${p.found} 篇`, `${p.screened} of ${p.found} screened`)
        : tr(`已筛选 ${p.screened} 篇`, `${p.screened} screened`),
    );
  } else if (fetched > 0 && p.found > 0) parts.push(tr(`去重后 ${p.found} 篇待筛选`, `${p.found} to screen after merging duplicates`));
  if (p.screened > 0) parts.push(tr(`留下 ${p.kept} 篇`, `${p.kept} kept`));
  if (p.to_summarise > 0 && p.summarised > 0) {
    // 重新检索后库里已有的解读可能比这次打算写的多：分母不小于分子，不出现「10/8」
    const total = Math.max(p.to_summarise, p.summarised);
    parts.push(tr(`已写解读 ${p.summarised}/${total}`, `${p.summarised} of ${total} summarised`));
  }
  return parts.join(' · ');
}

/**
 * 文献这一段做完后的一句结果，先说留下的：「留下 26 篇，其中 13 篇有解读」。
 * 怎么来的在第二句（surveyFunnelText）。
 */
export function surveyDoneText(p: SurveyProgress): string {
  if (p.kept === 0) return surveyProgressText(p);
  const head = tr(`留下 ${p.kept} 篇`, `${p.kept} ${p.kept === 1 ? 'paper' : 'papers'} kept`);
  if (p.summarised <= 0) return head;
  return p.summarised >= p.kept
    ? head + tr('，都有解读', ', all summarised')
    : head + tr(`，其中 ${p.summarised} 篇有解读`, `, ${p.summarised} with a summary`);
}

/**
 * 这些论文是怎么筛出来的：「检索到 354 条，去重后筛选了 85 篇，留下 26 篇」。
 * 没有可说的（什么都没筛掉、也不知道检索到多少）返回空串。
 */
export function surveyFunnelText(p: SurveyProgress): string {
  if (p.kept === 0) return '';
  const fetched = fetchedOf(p);
  const found = Math.max(p.found, p.kept);
  if (fetched > 0) {
    return tr(
      `检索到 ${fetched} 条，去重后筛选了 ${found} 篇，留下 ${p.kept} 篇`,
      `${fetched} results fetched, ${found} screened after merging duplicates, ${p.kept} kept`,
    );
  }
  if (found > p.kept) return tr(`筛选了 ${found} 篇，留下 ${p.kept} 篇`, `${found} screened, ${p.kept} kept`);
  return '';
}

export interface QueryGroup {
  source: string;
  name: string;
  /** 这个来源一共返回多少条 */
  returned: number;
  queries: SurveyQuery[];
}

/** 检索式按来源分组（来源的顺序 = 第一次出现的顺序）。 */
export function groupQueries(queries: SurveyQuery[] | null | undefined): QueryGroup[] {
  const out: QueryGroup[] = [];
  for (const q of queries ?? []) {
    let g = out.find((x) => x.source === q.source);
    if (!g) {
      g = { source: q.source, name: sourceName(q.source), returned: 0, queries: [] };
      out.push(g);
    }
    g.queries.push(q);
    g.returned += q.returned || 0;
  }
  return out;
}

/** 排序方式的说法：relevance → 按相关度，date / submittedDate → 按时间。认不出的原样。 */
export function querySortText(sort: string | null | undefined): string {
  const s = (sort ?? '').toLowerCase();
  if (!s) return '';
  if (s.includes('relev')) return tr('按相关度', 'by relevance');
  if (s.includes('date') || s.includes('recent') || s.includes('time')) return tr('按时间', 'by date');
  if (s.includes('cit')) return tr('按被引次数', 'by citations');
  return sort ?? '';
}

/** 时间范围的说法：「2016..2026」「2016-11-20..2026-11-18」→「2016–2026」。 */
export function queryWindowText(window: string | null | undefined): string {
  const w = (window ?? '').trim();
  if (!w) return '';
  const m = /^(\d{4})[\d-]*\.\.(\d{4})[\d-]*$/.exec(w);
  return m ? `${m[1]}–${m[2]}` : w;
}

/** 「通常需要 30–50 分钟」。 */
export function estimateText(e: Pick<ResearchEstimate, 'minutes_low' | 'minutes_high'>): string {
  const lo = Math.round(e.minutes_low ?? 0);
  const hi = Math.round(e.minutes_high ?? 0);
  if (lo <= 0 || hi <= 0) return '';
  return lo === hi
    ? tr(`通常需要约 ${hi} 分钟`, `Usually takes about ${hi} min`)
    : tr(`通常需要 ${lo}–${hi} 分钟`, `Usually takes ${lo}–${hi} min`);
}

/**
 * 一次研究一共花了多久：确定范围 + 检索文献 + 寻找假设三段各自的时长加起来
 * （不是从创建到现在——选想法想了两天不算）。有哪一段没结束就返回 null。
 */
export function totalDurationMs(run: {
  created_at: string;
  survey: { started_at: string | null; finished_at: string | null };
  hypotheses: { started_at: string | null; finished_at: string | null };
}): number | null {
  const t = (iso: string | null) => (iso ? parseApiDate(iso).getTime() : NaN);
  const s0 = t(run.survey.started_at);
  const s1 = t(run.survey.finished_at);
  const h0 = t(run.hypotheses.started_at);
  const h1 = t(run.hypotheses.finished_at);
  const c = t(run.created_at);
  if ([s0, s1, h0, h1].some((x) => Number.isNaN(x)) || s1 < s0 || h1 < h0) return null;
  const plan = Number.isNaN(c) ? 0 : Math.max(0, s0 - c);
  return plan + (s1 - s0) + (h1 - h0);
}

/** 一段的用量，跟在时长后面：「约 $5.07」；还没有或不到一分钱不写。 */
export function costText(usage: ResearchUsage | null | undefined): string {
  if (!usage || !(usage.cost_usd >= 0.01)) return '';
  return tr(`约 $${usage.cost_usd.toFixed(2)}`, `about $${usage.cost_usd.toFixed(2)}`);
}

/** 「5 篇支持 · 1 篇反对 · 1 处尚无依据」；为 0 的不写，全为 0 时说明没有依据。 */
export function evidenceText(c: { support: number; refute: number; unsupported: number }): string {
  const parts: string[] = [];
  if (c.support > 0) parts.push(tr(`${c.support} 篇支持`, `${c.support} supporting`));
  if (c.refute > 0) parts.push(tr(`${c.refute} 篇反对`, `${c.refute} against`));
  if (c.unsupported > 0) parts.push(tr(`${c.unsupported} 处尚无依据`, `${c.unsupported} unsupported`));
  return parts.length > 0 ? parts.join(' · ') : tr('没有文献依据', 'No evidence from the papers');
}

/** 距离自动重试还有多久（按分钟向上取整）。到点或已过返回 0。 */
export function minutesUntil(iso: string | null | undefined, nowMs: number): number {
  if (!iso) return 0;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return 0;
  return Math.max(0, Math.ceil((t - nowMs) / 60_000));
}

/** 临时故障的原因，半句话（后面要接「，约 3 分钟后自动重试」）。认不出的码只说「遇到临时问题」。 */
export function transientReason(code: string | null | undefined): string {
  switch (code) {
    case 'NETWORK':
      return tr('网络连接失败', 'The network connection failed');
    case 'TIMEOUT':
      return tr('请求超时', 'A request timed out');
    case 'RATE_LIMITED':
      return tr('文献来源暂时限流', 'A paper source is rate-limiting requests');
    case 'UPSTREAM_UNAVAILABLE':
      return tr('文献来源暂时不可用', 'A paper source is temporarily unavailable');
    case 'LLM_UNREACHABLE':
      return tr('暂时连不上模型服务', 'The model service can’t be reached right now');
    case 'LLM_RATE_LIMITED':
      return tr('模型服务暂时限流', 'The model service is rate-limiting requests');
    case 'LLM_OVERLOADED':
      return tr('模型服务暂时繁忙', 'The model service is busy');
    case 'DB_BUSY':
      return tr('本地数据正忙', 'Local data is busy');
    default:
      return tr('遇到临时问题', 'Hit a temporary problem');
  }
}

/** 「约 4 分钟后自动重试（第 2 次，共 3 次）」。 */
export function retryText(nextRetryAt: string | null | undefined, attempt: number | null | undefined, nowMs: number): string {
  const n = minutesUntil(nextRetryAt, nowMs);
  const when =
    n <= 0
      ? tr('马上自动重试', 'Retrying shortly')
      : n === 1
        ? tr('约 1 分钟后自动重试', 'Retrying in about 1 min')
        : tr(`约 ${n} 分钟后自动重试`, `Retrying in about ${n} min`);
  if (!attempt || attempt < 1) return when;
  return tr(`${when}（第 ${attempt} 次，共 3 次）`, `${when} (attempt ${attempt} of 3)`);
}

/** 来源 id → 给人看的名字。认不出的原样显示（插件装的新来源也看得出是哪个）。 */
const SOURCE_NAMES: Record<string, string> = {
  arxiv: 'arXiv',
  openalex: 'OpenAlex',
  semantic: 'Semantic Scholar',
  pubmed: 'PubMed',
  europepmc: 'Europe PMC',
  crossref: 'Crossref',
  hal: 'HAL',
  core: 'CORE',
  base: 'BASE',
  sciverse: 'ScienceDirect',
  unpaywall: 'Unpaywall',
};

export function sourceName(id: string): string {
  return SOURCE_NAMES[id] ?? id;
}

export interface SourceLine {
  id: string;
  name: string;
  /** 「120 篇」/「没有响应」 */
  text: string;
  failed: boolean;
  /** 引擎给的原始错误，放进悬停提示 */
  detail: string | null;
}

/** 各来源这次的结果，照实说：拿到几篇，或者没连上。 */
export function sourceLines(sources: SurveySource[]): SourceLine[] {
  return sources.map((s) => {
    const failed = !!s.error && s.fetched === 0;
    return {
      id: s.source,
      name: sourceName(s.source),
      text: failed ? tr('没取到', 'failed') : tr(`${s.fetched} 条`, String(s.fetched)),
      failed,
      detail: s.error || null,
    };
  });
}

/** 时长的说法：不到 1 分钟 / 12 分钟 / 1 小时 5 分钟。 */
export function durationText(ms: number): string {
  const min = Math.floor(Math.max(0, ms) / 60_000);
  if (min < 1) return tr('不到 1 分钟', 'under a minute');
  if (min < 60) return tr(`${min} 分钟`, `${min} min`);
  const h = Math.floor(min / 60);
  const m = min % 60;
  return m === 0 ? tr(`${h} 小时`, `${h} h`) : tr(`${h} 小时 ${m} 分钟`, `${h} h ${m} min`);
}

/**
 * 一个阶段花了多久：进行中是「已用 12 分钟」，结束后是「用时 12 分钟」；还没开始返回空串。
 * 时刻都来自引擎（started_at / finished_at），刷新、重启后不会从头数。
 */
export function elapsedText(
  startedAt: string | null | undefined,
  finishedAt: string | null | undefined,
  nowMs: number,
): string {
  if (!startedAt) return '';
  const start = parseApiDate(startedAt).getTime();
  if (Number.isNaN(start)) return '';
  if (finishedAt) {
    const end = parseApiDate(finishedAt).getTime();
    if (Number.isNaN(end) || end < start) return '';
    if (end - start < 60_000) return tr('用时不到 1 分钟', 'Took under a minute');
    return tr(`用时 ${durationText(end - start)}`, `Took ${durationText(end - start)}`);
  }
  if (nowMs - start < 60_000) return tr('刚开始', 'Just started');
  return tr(`已用 ${durationText(nowMs - start)}`, `${durationText(nowMs - start)} so far`);
}

/** 「用量约 $0.77」；还没有用量时返回 null。提示里给 token 数。 */
export function usageText(usage: ResearchUsage | null | undefined): { text: string; title: string } | null {
  if (!usage || usage.total_tokens <= 0) return null;
  const small = usage.cost_usd < 0.01;
  return {
    text: small
      ? tr('用量不到 $0.01', 'Usage under $0.01')
      : tr(`用量约 $${usage.cost_usd.toFixed(2)}`, `Usage about $${usage.cost_usd.toFixed(2)}`),
    title: tr(
      `${fmtTokens(usage.total_tokens)} tokens，按 Claude 的标价估算`,
      `${fmtTokens(usage.total_tokens)} tokens, estimated at Claude’s list price`,
    ),
  };
}

/** 「46 篇论文 · 5 个想法」；为 0 的不写。 */
export function countsText(counts: { papers: number; ideas: number }): string {
  const parts: string[] = [];
  if (counts.papers > 0) parts.push(tr(`${counts.papers} 篇论文`, `${counts.papers} ${counts.papers === 1 ? 'paper' : 'papers'}`));
  if (counts.ideas > 0) parts.push(tr(`${counts.ideas} 个想法`, `${counts.ideas} ${counts.ideas === 1 ? 'idea' : 'ideas'}`));
  return parts.join(' · ');
}
