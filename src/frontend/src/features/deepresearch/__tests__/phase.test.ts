import { describe, expect, it } from 'vitest';
import { ApiError } from '../../../lib/api';
import { errorText } from '../../../lib/errors';
import { isEngineUnreachable } from '../../../lib/researchApi';
import {
  attentionActions,
  attentionSection,
  attentionText,
  countsText,
  durationText,
  elapsedText,
  evidenceText,
  minutesUntil,
  pollInterval,
  retryText,
  runHeadline,
  sectionStates,
  sourceLines,
  surveyDoneText,
  surveyFraction,
  surveyProgressText,
  surveyStage,
  surveyStageCaption,
  surveyStageLabel,
  SURVEY_STAGES,
  usageText,
  waitingFor,
} from '../phase';

/* 一次研究的 (phase, status, attention) → 四段各自的状态、用户要做的事、给人看的句子。 */

describe('sectionStates', () => {
  it('marks the current phase running and everything before it done', () => {
    expect(sectionStates({ phase: 'plan', status: 'running' })).toEqual({
      scope: 'running', papers: 'pending', ideas: 'pending', next: 'pending',
    });
    expect(sectionStates({ phase: 'survey', status: 'running' })).toEqual({
      scope: 'done', papers: 'running', ideas: 'pending', next: 'pending',
    });
    expect(sectionStates({ phase: 'hypotheses', status: 'running' })).toEqual({
      scope: 'done', papers: 'done', ideas: 'running', next: 'pending',
    });
  });

  it('shows a pending auto-retry as retrying, not as a failure', () => {
    const s = sectionStates({ phase: 'survey', status: 'running', next_retry_at: '2026-11-18T10:00:00Z' });
    expect(s.papers).toBe('retrying');
    expect(s.scope).toBe('done');
  });

  it('puts both kinds of waiting on the ideas section', () => {
    expect(sectionStates({ phase: 'hypotheses', status: 'waiting_user' }).ideas).toBe('waiting');
    const choose = sectionStates({ phase: 'choose', status: 'waiting_user' });
    expect(choose).toEqual({ scope: 'done', papers: 'done', ideas: 'waiting', next: 'pending' });
  });

  it('opens the next section only once the run is done', () => {
    expect(sectionStates({ phase: 'choose', status: 'done' })).toEqual({
      scope: 'done', papers: 'done', ideas: 'done', next: 'ready',
    });
  });

  it('places attention on the section the reason belongs to, whatever the phase says', () => {
    // 文献太少是在准备寻找假设时才发现的，但要用户做的事在「文献」这一段
    const s = sectionStates({ phase: 'hypotheses', status: 'needs_attention', attention: { code: 'LIBRARY_TOO_SMALL' } });
    expect(s).toEqual({ scope: 'done', papers: 'attention', ideas: 'pending', next: 'pending' });
    expect(sectionStates({ phase: 'plan', status: 'needs_attention', attention: { code: 'PLAN_FAILED' } }).scope).toBe('attention');
    expect(
      sectionStates({ phase: 'hypotheses', status: 'needs_attention', attention: { code: 'NO_HYPOTHESES_SURVIVED' } }).ideas,
    ).toBe('attention');
  });

  it('keeps model failures on whichever phase hit them', () => {
    expect(attentionSection('LLM_AUTH', 'plan')).toBe('scope');
    expect(attentionSection('LLM_AUTH', 'survey')).toBe('papers');
    expect(attentionSection('LLM_UNREACHABLE', 'hypotheses')).toBe('ideas');
    expect(attentionSection('SOMETHING_NEW', 'survey')).toBe('papers');
  });

  it('stops at the phase the user cancelled in and keeps earlier results', () => {
    expect(sectionStates({ phase: 'survey', status: 'cancelled' })).toEqual({
      scope: 'done', papers: 'stopped', ideas: 'pending', next: 'pending',
    });
  });

  it('never draws an unknown status or phase as finished', () => {
    const s = sectionStates({ phase: 'brand_new', status: 'brand_new' });
    expect(Object.values(s)).not.toContain('done');
    expect(Object.values(s)).not.toContain('ready');
  });
});

describe('attention', () => {
  it('takes the primary action from the engine', () => {
    // 这些 (code, action) 就是引擎实际给出的组合（services/research_runs.py）
    expect(attentionActions({ code: 'PLAN_FAILED', action: 'retry' })).toEqual({ primary: 'retry' });
    expect(attentionActions({ code: 'SURVEY_SOURCES_UNREACHABLE', action: 'retry' })).toEqual({ primary: 'retry' });
    expect(attentionActions({ code: 'SURVEY_NO_RESULTS', action: 'edit_scope' })).toEqual({ primary: 'edit_scope', secondary: 'retry' });
    expect(attentionActions({ code: 'SURVEY_TOO_FEW_RELEVANT', action: 'broaden' })).toEqual({ primary: 'broaden', secondary: 'add_papers' });
    expect(attentionActions({ code: 'SURVEY_FAILED', action: 'retry' })).toEqual({ primary: 'retry' });
    expect(attentionActions({ code: 'LIBRARY_TOO_SMALL', action: 'retry' })).toEqual({ primary: 'retry', secondary: 'add_papers' });
    expect(attentionActions({ code: 'NO_HYPOTHESES_SURVIVED', action: 'add_papers' })).toEqual({ primary: 'add_papers', secondary: 'retry' });
    expect(attentionActions({ code: 'HYPOTHESES_FAILED', action: 'retry' })).toEqual({ primary: 'retry' });
    // 引擎换了主意，界面跟着换
    expect(attentionActions({ code: 'SURVEY_FAILED', action: 'edit_scope' }).primary).toBe('edit_scope');
  });

  it('sends a rejected key to Settings, because retrying cannot help', () => {
    expect(attentionActions({ code: 'LLM_AUTH', action: 'retry' })).toEqual({ primary: 'settings', secondary: 'retry' });
  });

  it('guesses from the code when the engine gives no action it knows', () => {
    expect(attentionActions({ code: 'SURVEY_NO_RESULTS' }).primary).toBe('edit_scope');
    expect(attentionActions({ code: 'SURVEY_TOO_FEW_RELEVANT', action: null }).primary).toBe('broaden');
    expect(attentionActions({ code: 'NO_HYPOTHESES_SURVIVED', action: 'something_new' }).primary).toBe('add_papers');
    expect(attentionActions({ code: 'SOMETHING_NEW' })).toEqual({ primary: 'retry' });
  });

  it('writes the counts into the sentence when it has them', () => {
    expect(attentionText({ code: 'SURVEY_TOO_FEW_RELEVANT', counts: { found: 38, relevant: 2 } })).toBe(
      '找到 38 篇，只有 2 篇相关，请放宽范围',
    );
    expect(attentionText({ code: 'LIBRARY_TOO_SMALL', counts: { papers: 3, required: 5 } })).toBe(
      '文献库里只有 3 篇论文，至少需要 5 篇',
    );
  });

  it('falls back to the plain sentence without counts', () => {
    expect(attentionText({ code: 'SURVEY_TOO_FEW_RELEVANT' })).toBe('相关的论文太少，请放宽范围');
    expect(attentionText({ code: 'SURVEY_NO_RESULTS' })).toBe('没有检索到论文，请调整关键词');
    expect(attentionText(null)).toBe('');
  });

  it('has a plain sentence for every code, including the one that arrives with a suffix', () => {
    const codes = [
      'SURVEY_SOURCES_UNREACHABLE', 'SURVEY_NO_RESULTS', 'SURVEY_TOO_FEW_RELEVANT', 'SURVEY_FAILED',
      'HYPOTHESES_FAILED', 'LIBRARY_TOO_SMALL', 'NO_HYPOTHESES_SURVIVED', 'PLAN_FAILED', 'LLM_UNREACHABLE',
      'LLM_AUTH', 'RESEARCH_RUN_NOT_FOUND',
    ];
    for (const code of codes) {
      const text = errorText(new ApiError(409, code));
      expect(text).not.toContain(code);
      expect(text).not.toMatch(/后端|服务端|backend|server/i);
    }
    expect(errorText(new ApiError(409, 'LIBRARY_TOO_SMALL:3'))).toBe('文献库里的论文太少，至少需要 5 篇');
  });
});

describe('headline and waiting', () => {
  it('says what the user has to do', () => {
    expect(waitingFor({ phase: 'hypotheses', status: 'waiting_user' })).toBe('answer');
    expect(waitingFor({ phase: 'choose', status: 'waiting_user' })).toBe('pick');
    expect(waitingFor({ phase: 'survey', status: 'needs_attention' })).toBe('attention');
    expect(waitingFor({ phase: 'survey', status: 'running' })).toBeNull();
    expect(waitingFor({ phase: 'choose', status: 'done' })).toBeNull();
  });

  it('labels a running run by its phase and the rest by status', () => {
    expect(runHeadline({ phase: 'plan', status: 'running' })).toEqual({ tone: 'active', label: '确定范围' });
    expect(runHeadline({ phase: 'survey', status: 'running' })).toEqual({ tone: 'active', label: '检索文献' });
    expect(runHeadline({ phase: 'hypotheses', status: 'running' })).toEqual({ tone: 'active', label: '寻找假设' });
    expect(runHeadline({ phase: 'hypotheses', status: 'waiting_user' })).toEqual({ tone: 'warn', label: '等你回答' });
    expect(runHeadline({ phase: 'choose', status: 'waiting_user' })).toEqual({ tone: 'warn', label: '等你选定想法' });
    expect(runHeadline({ phase: 'survey', status: 'needs_attention' })).toEqual({ tone: 'warn', label: '需要处理' });
    expect(runHeadline({ phase: 'choose', status: 'done' })).toEqual({ tone: 'ok', label: '已完成' });
    expect(runHeadline({ phase: 'survey', status: 'cancelled' })).toEqual({ tone: 'idle', label: '已停止' });
  });

  it('polls fast while running, slowly while waiting, not at all otherwise', () => {
    expect(pollInterval('running')).toBe(5000);
    expect(pollInterval('waiting_user')).toBe(30000);
    expect(pollInterval('needs_attention')).toBe(false);
    expect(pollInterval('done')).toBe(false);
    expect(pollInterval('cancelled')).toBe(false);
    expect(pollInterval(undefined)).toBe(false);
  });
});

describe('survey progress', () => {
  const p = (found: number, screened: number, kept: number, summarised: number, to: number) => ({
    found, screened, kept, summarised, to_summarise: to,
  });

  it('derives the sub-step from the counts alone', () => {
    expect(surveyStage(p(0, 0, 0, 0, 0))).toBe('search');
    expect(surveyStage(p(120, 0, 0, 0, 0))).toBe('search');
    expect(surveyStage(p(182, 60, 14, 0, 14))).toBe('screen');
    // 筛完了、还没有一篇解读：在读全文
    expect(surveyStage(p(182, 182, 46, 0, 46))).toBe('fulltext');
    expect(surveyStage(p(182, 182, 46, 12, 46))).toBe('summarise');
  });

  it('lets the step the engine reports win over the counts', () => {
    expect(surveyStage(p(182, 182, 46, 0, 46), 'wiki.compile')).toBe('summarise');
    expect(surveyStage(p(182, 182, 46, 46, 46), 'wiki.link_concepts')).toBe('index');
    expect(surveyStage(p(0, 0, 0, 0, 0), 'wiki.score_relevance')).toBe('screen');
    // 认不出的动作：回到按数字判断
    expect(surveyStage(p(182, 60, 14, 0, 14), 'wiki.something_new')).toBe('screen');
    expect(surveyStage(p(182, 60, 14, 0, 14), null)).toBe('screen');
  });

  it('has a fraction only for the steps that have a total', () => {
    expect(surveyFraction(p(120, 0, 0, 0, 0))).toBeNull();
    expect(surveyFraction(p(200, 50, 10, 0, 10))).toBe(0.25);
    expect(surveyFraction(p(182, 182, 46, 23, 46))).toBe(0.5);
    expect(surveyFraction(p(182, 182, 46, 0, 46), 'fulltext')).toBeNull();
    expect(surveyFraction(p(182, 182, 46, 46, 46), 'index')).toBeNull();
  });

  it('says what each step is doing in a plain sentence, never a log line', () => {
    expect(surveyStageCaption('search', p(0, 0, 0, 0, 0), ['arXiv', 'OpenAlex'])).toBe('正在 arXiv、OpenAlex 检索论文');
    expect(surveyStageCaption('search', p(0, 0, 0, 0, 0), [])).toBe('正在检索论文');
    expect(surveyStageCaption('summarise', p(182, 182, 46, 3, 46), [])).toBe('正在为最相关的 46 篇写解读');
    for (const stage of SURVEY_STAGES) {
      expect(surveyStageLabel(stage)).not.toMatch(/LLM|PDF|wiki|embedding/i);
      expect(surveyStageCaption(stage, p(10, 10, 5, 0, 5), ['arXiv'])).not.toMatch(/LLM|wiki|embedding|▶/i);
    }
  });

  it('leaves out numbers that do not exist yet', () => {
    expect(surveyProgressText(p(0, 0, 0, 0, 0))).toBe('');
    expect(surveyProgressText(p(120, 0, 0, 0, 0))).toBe('找到 120 篇');
    expect(surveyProgressText(p(182, 140, 30, 0, 30))).toBe('找到 182 篇 · 已筛选 140 · 留下 30');
    expect(surveyProgressText(p(182, 182, 46, 0, 46))).toBe('找到 182 篇 · 已筛选 182 · 留下 46');
    expect(surveyProgressText(p(182, 182, 46, 12, 46))).toBe('找到 182 篇 · 已筛选 182 · 留下 46 · 已写解读 12/46');
  });

  it('never shows more summaries than the total', () => {
    // 重新检索后：库里已有 10 篇解读，这次只打算写 8 篇
    expect(surveyProgressText(p(10, 10, 10, 10, 8))).toContain('已写解读 10/10');
  });

  it('starts a fresh task at Search even when the library already has numbers', () => {
    expect(surveyStage(p(8, 8, 8, 8, 8), null, 'planning')).toBe('search');
    expect(surveyStage(p(8, 8, 8, 8, 8), 'wiki.compile', 'executing')).toBe('summarise');
  });

  it('sums a finished survey up as a result, not as a process', () => {
    expect(surveyDoneText(p(182, 182, 46, 46, 46))).toBe('留下 46 篇（共找到 182 篇），都有解读');
    expect(surveyDoneText(p(193, 193, 193, 60, 60))).toBe('留下 193 篇，其中 60 篇有解读');
    expect(surveyDoneText(p(10, 10, 10, 0, 8))).toBe('留下 10 篇');
  });
});

describe('sources, time and usage', () => {
  it('says honestly what each source returned', () => {
    const lines = sourceLines([
      { source: 'openalex', fetched: 10, error: null },
      { source: 'arxiv', fetched: 0, error: 'ArxivRateLimitedError: HTTP 429' },
      { source: 'my_plugin', fetched: 0, error: null },
    ]);
    expect(lines.map((l) => `${l.name} ${l.text}`)).toEqual(['OpenAlex 10 篇', 'arXiv 没取到', 'my_plugin 0 篇']);
    expect(lines.map((l) => l.failed)).toEqual([false, true, false]);
    expect(lines[1]?.detail).toContain('429');
  });

  it('does not call a source failed when it still returned papers', () => {
    const [line] = sourceLines([{ source: 'arxiv', fetched: 40, error: 'timeout on page 3' }]);
    expect(line?.failed).toBe(false);
    expect(line?.text).toBe('40 篇');
  });

  it('counts elapsed time from the engine’s timestamps', () => {
    const now = Date.parse('2026-11-18T10:12:30Z');
    expect(elapsedText('2026-11-18T10:00:00Z', null, now)).toBe('已用 12 分钟');
    expect(elapsedText('2026-11-18T10:12:10Z', null, now)).toBe('刚开始');
    expect(elapsedText('2026-11-18T10:12:00Z', '2026-11-18T10:12:20Z', now)).toBe('用时不到 1 分钟');
    expect(elapsedText('2026-11-18T08:00:00Z', '2026-11-18T09:05:00Z', now)).toBe('用时 1 小时 5 分钟');
    expect(elapsedText(null, null, now)).toBe('');
    expect(elapsedText('2026-11-18T10:00:00Z', '2026-11-18T09:00:00Z', now)).toBe('');
    expect(durationText(3 * 3_600_000)).toBe('3 小时');
  });

  it('shows usage as an approximate cost, and nothing before there is any', () => {
    expect(usageText({ prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, cost_usd: 0 })).toBeNull();
    expect(usageText(null)).toBeNull();
    const u = usageText({ prompt_tokens: 103520, completion_tokens: 10201, total_tokens: 113721, cost_usd: 0.77 });
    expect(u?.text).toBe('用量约 $0.77');
    expect(u?.title).toContain('113.7k tokens');
    expect(usageText({ prompt_tokens: 300, completion_tokens: 20, total_tokens: 320, cost_usd: 0 })?.text).toBe('用量不到 $0.01');
  });
});

describe('small sentences', () => {
  it('writes the evidence line without zero parts', () => {
    expect(evidenceText({ support: 5, refute: 1, unsupported: 1 })).toBe('5 篇支持 · 1 篇反对 · 1 处尚无依据');
    expect(evidenceText({ support: 6, refute: 0, unsupported: 0 })).toBe('6 篇支持');
    expect(evidenceText({ support: 0, refute: 0, unsupported: 0 })).toBe('没有文献依据');
  });

  it('counts papers and ideas', () => {
    expect(countsText({ papers: 46, ideas: 5 })).toBe('46 篇论文 · 5 个想法');
    expect(countsText({ papers: 46, ideas: 0 })).toBe('46 篇论文');
    expect(countsText({ papers: 0, ideas: 0 })).toBe('');
  });

  it('rounds the wait up to whole minutes and never goes negative', () => {
    const now = Date.parse('2026-11-18T10:00:00Z');
    expect(minutesUntil('2026-11-18T10:04:20Z', now)).toBe(5);
    expect(minutesUntil('2026-11-18T10:00:30Z', now)).toBe(1);
    expect(minutesUntil('2026-11-18T09:59:00Z', now)).toBe(0);
    expect(minutesUntil(null, now)).toBe(0);
    expect(minutesUntil('not a date', now)).toBe(0);
  });

  it('says when the retry happens and which attempt it is', () => {
    const now = Date.parse('2026-11-18T10:00:00Z');
    expect(retryText('2026-11-18T10:04:20Z', 2, now)).toBe('约 5 分钟后自动重试（第 2 次，共 3 次）');
    expect(retryText('2026-11-18T10:00:40Z', null, now)).toBe('约 1 分钟后自动重试');
    expect(retryText('2026-11-18T09:59:00Z', 0, now)).toBe('马上自动重试');
  });
});

describe('isEngineUnreachable', () => {
  it('recognises a request that never reached the engine', () => {
    expect(isEngineUnreachable(new TypeError('Failed to fetch'))).toBe(true);
    expect(isEngineUnreachable(new ApiError(502, 'Bad Gateway'))).toBe(true);
    expect(isEngineUnreachable(new ApiError(500, 'Internal Server Error'))).toBe(true);
  });

  it('does not mistake a real answer from the engine for an outage', () => {
    expect(isEngineUnreachable(new ApiError(404, 'RESEARCH_RUN_NOT_FOUND', { detail: 'RESEARCH_RUN_NOT_FOUND' }))).toBe(false);
    expect(isEngineUnreachable(new ApiError(500, 'boom', { detail: 'boom' }))).toBe(false);
    expect(isEngineUnreachable(new Error('x'))).toBe(false);
  });
});
