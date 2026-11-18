import { errorText } from '../../lib/errors';
import { tr } from '../../lib/i18n';
import type {
  ResearchAttention,
  ResearchPhase,
  ResearchRunRead,
  ResearchStatus,
  SurveyProgress,
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

/** 每个原因只有一个主动作；个别原因多给一个次要动作（不抢主按钮）。 */
export function attentionActions(code: string): { primary: AttentionAction; secondary?: AttentionAction } {
  switch (code) {
    case 'SURVEY_NO_RESULTS':
      return { primary: 'edit_scope' };
    case 'SURVEY_TOO_FEW_RELEVANT':
      return { primary: 'broaden', secondary: 'add_papers' };
    case 'LIBRARY_TOO_SMALL':
    case 'NO_HYPOTHESES_SURVIVED':
      // 不加论文直接重试多半还是同样的结果，所以「添加论文」是主动作
      return { primary: 'add_papers', secondary: 'retry' };
    case 'LLM_AUTH':
      return { primary: 'settings', secondary: 'retry' };
    default:
      return { primary: 'retry' };
  }
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

/** 卡住的原因 → 一句「什么没成功 + 怎么办」。带数字的原因把数字写进去。 */
export function attentionText(attention: ResearchAttention | null | undefined): string {
  if (!attention) return '';
  const c = attention.counts ?? {};
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
  return run.status === 'needs_attention' ? attentionText(run.attention) : '';
}

/** 每一段右上角的状态词；pending 不写字（留白比「未开始」安静）。 */
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
    case 'done':
      return { tone: 'ok', label: tr('已完成', 'Done') };
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

/** 文献检索走到哪个小步：检索 → 筛选 → 生成解读。只看库里的数字，刷新、重启后一致。 */
export type SurveyStage = 'search' | 'screen' | 'summarise';

export function surveyStage(p: SurveyProgress): SurveyStage {
  if (p.found === 0) return 'search';
  if (p.summarised > 0) return 'summarise';
  if (p.screened >= p.found && p.to_summarise > 0) return 'summarise';
  if (p.screened > 0) return 'screen';
  return 'search';
}

/** 当前小步的完成比例（0..1）；检索阶段没有总数，返回 null（画不定进度）。 */
export function surveyFraction(p: SurveyProgress): number | null {
  const stage = surveyStage(p);
  if (stage === 'search') return null;
  if (stage === 'screen') return p.found > 0 ? Math.min(1, p.screened / p.found) : null;
  return p.to_summarise > 0 ? Math.min(1, p.summarised / p.to_summarise) : null;
}

/** 「找到 182 篇 · 已筛选 140 · 留下 46 · 已生成解读 12/46」；还没有的数字不写。 */
export function surveyProgressText(p: SurveyProgress): string {
  const parts: string[] = [];
  if (p.found > 0) parts.push(tr(`找到 ${p.found} 篇`, `${p.found} found`));
  if (p.screened > 0) parts.push(tr(`已筛选 ${p.screened}`, `${p.screened} screened`));
  if (p.screened > 0) parts.push(tr(`留下 ${p.kept}`, `${p.kept} kept`));
  if (p.to_summarise > 0 && (p.summarised > 0 || p.screened >= p.found)) {
    parts.push(tr(`已生成解读 ${p.summarised}/${p.to_summarise}`, `${p.summarised} of ${p.to_summarise} summarised`));
  }
  return parts.join(' · ');
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

/** 「46 篇论文 · 5 个想法」；为 0 的不写。 */
export function countsText(counts: { papers: number; ideas: number }): string {
  const parts: string[] = [];
  if (counts.papers > 0) parts.push(tr(`${counts.papers} 篇论文`, `${counts.papers} ${counts.papers === 1 ? 'paper' : 'papers'}`));
  if (counts.ideas > 0) parts.push(tr(`${counts.ideas} 个想法`, `${counts.ideas} ${counts.ideas === 1 ? 'idea' : 'ideas'}`));
  return parts.join(' · ');
}
