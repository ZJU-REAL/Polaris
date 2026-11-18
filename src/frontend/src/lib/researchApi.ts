import { ApiError, request, requestJson, type IdeaScores, type IdeaStatus, type VoyageMessageRead } from './api';

/* ============================================================
   研究（Auto DeepResearch）的接口：一个方向 = 一个课题 = 一次研究。

   单独成文件，不往 5k 行的 lib/api.ts 里加：那边同时有别的改动在进行。
   后端只有 research_runs 一张表在推进阶段，文献检索与假设探索各是一个
   子任务（voyage）；这里的类型就是 `GET /research/{id}` 拼好的那一份。
   ============================================================ */

/** 研究走到哪一步：确定范围 → 检索文献 → 寻找假设 → 等你选定想法。 */
export type ResearchPhase = 'plan' | 'survey' | 'hypotheses' | 'choose';

export type ResearchStatus = 'running' | 'waiting_user' | 'needs_attention' | 'done' | 'cancelled';

/** 需要处理时的原因。已知取值见 features/deepresearch/phase.ts；认不出的按「这一步没有完成」处理。 */
export interface ResearchAttention {
  code: string;
  /** 原始信息（等宽小字给排查的人看），不是给人读的句子 */
  detail?: string | null;
  /** 随原因而变：SURVEY_TOO_FEW_RELEVANT 带 {found, relevant}，LIBRARY_TOO_SMALL 带 {papers, required} */
  counts?: Record<string, number> | null;
}

/** 检索范围：由模型从一句方向推出来，用户可以在开始前或重新检索前改。 */
export interface ResearchBrief {
  title: string;
  language: 'zh' | 'en';
  statement: string;
  questions: string[];
  /** 英文检索词（文献来源按英文检索） */
  include_keywords: string[];
  exclude_keywords: string[];
  arxiv_categories: string[];
  discipline: string | null;
  sources: string[];
  in_scope: string[];
  out_of_scope: string[];
  /** 12 | 36 | 60 */
  months_back: number;
  /** 最多读多少篇；不带就用默认（200）。契约 §2.2 里没有这一项，见 F2 的说明。 */
  max_papers?: number | null;
}

export interface ResearchCounts {
  papers: number;
  ideas: number;
  picked: number;
}

export interface ResearchRunRead {
  id: string;
  project_id: string;
  /** 范围确定之前为 null */
  library_id: string | null;
  name: string;
  /** 用户输入的原话 */
  direction: string;
  phase: ResearchPhase;
  status: ResearchStatus;
  attention: ResearchAttention | null;
  counts: ResearchCounts;
  updated_at: string;
}

export interface SurveyProgress {
  found: number;
  screened: number;
  kept: number;
  summarised: number;
  to_summarise: number;
}

export interface SurveyPaper {
  id: string;
  title: string;
  year: number | null;
  tldr: string | null;
  relevance: number | null;
}

export interface ResearchSurvey {
  /** 还没开始为 null */
  voyage_id: string | null;
  /** 子任务的状态原样透出；页面只把它当提示用，阶段状态以 phase + status 为准 */
  state: string | null;
  step_title: string | null;
  progress: SurveyProgress;
  top_papers: SurveyPaper[];
  /** 检索过程中值得告诉用户的事（如「某个来源没有响应」），已经是给人读的句子 */
  notes: string[];
}

export interface ResearchHypotheses {
  voyage_id: string | null;
  state: string | null;
  round: number;
  max_rounds: number;
  node_count: number;
}

export interface EvidenceCounts {
  support: number;
  refute: number;
  unsupported: number;
}

export interface ResearchIdea {
  id: string;
  title: string;
  summary: string;
  scores: IdeaScores | null;
  status: IdeaStatus;
  evidence_counts: EvidenceCounts;
  origin: { kind?: string; novelty_scope?: string; library_paper_count?: number; [extra: string]: unknown } | null;
}

/** 子任务为什么停下（引擎写的 last_error）。 */
export interface ResearchLastError {
  code: string;
  message: string;
  transient: boolean;
  source?: string | null;
}

export interface ResearchRunDetail extends ResearchRunRead {
  brief: ResearchBrief | null;
  survey: ResearchSurvey;
  hypotheses: ResearchHypotheses;
  ideas: ResearchIdea[];
  /** 当前子任务在等回答的提问 */
  open_ask: VoyageMessageRead | null;
  /** 排了自动重试时的时刻；有它时 status 仍是 running */
  next_retry_at: string | null;
  /** 即将进行的是第几次自动重试（1..3）。契约里没有，缺省时页面不显示次数。 */
  retry_attempt?: number | null;
  /** 当前子任务最近一次出错的原因。契约里没有，缺省时页面只说「遇到临时问题」。 */
  last_error?: ResearchLastError | null;
  capabilities: { remote_machine: boolean; latex: boolean };
}

export interface CreateRunInput {
  direction: string;
  /** 幂等键：同一个键重复提交返回同一次研究 */
  client_key: string;
  brief?: ResearchBrief;
}

export interface RetryRunInput {
  /** 带上就按这份范围重新检索 */
  brief?: ResearchBrief;
  /** SURVEY_TOO_FEW_RELEVANT 的「放宽范围」：降低相关性门槛、时间拉到 5 年。契约里没有这一项。 */
  broaden?: boolean;
}

export const researchApi = {
  /** 只生成范围、不创建任何东西；一次模型调用，要等几秒。 */
  draftBrief(direction: string): Promise<ResearchBrief> {
    return requestJson<ResearchBrief>('/research/brief', 'POST', { direction });
  },
  createRun(input: CreateRunInput): Promise<ResearchRunRead> {
    return requestJson<ResearchRunRead>('/research', 'POST', input);
  },
  listRuns(): Promise<ResearchRunRead[]> {
    return request<ResearchRunRead[]>('/research');
  },
  getRun(id: string): Promise<ResearchRunDetail> {
    return request<ResearchRunDetail>(`/research/${id}`);
  },
  /** 重试当前这一步；带 brief = 按新范围重新检索。 */
  retryRun(id: string, input: RetryRunInput = {}): Promise<ResearchRunRead> {
    return requestJson<ResearchRunRead>(`/research/${id}/retry`, 'POST', input);
  },
  cancelRun(id: string): Promise<ResearchRunRead> {
    return request<ResearchRunRead>(`/research/${id}/cancel`, { method: 'POST' });
  },
  moreIdeas(id: string): Promise<ResearchRunRead> {
    return requestJson<ResearchRunRead>(`/research/${id}/more-ideas`, 'POST', {});
  },
  renameRun(id: string, name: string): Promise<ResearchRunRead> {
    return requestJson<ResearchRunRead>(`/research/${id}`, 'PATCH', { name });
  },
  /** 选定 / 取消选定一个想法（两个方向都可逆）。 */
  pickIdea(ideaId: string, picked: boolean): Promise<unknown> {
    return requestJson<unknown>(`/ideas/${ideaId}`, 'PATCH', { status: picked ? 'promoted' : 'candidate' });
  },
};

/**
 * 请求根本没到引擎：桌面端是 fetch 抛 TypeError；开发时经 Vite 代理是 502/503/504，
 * 或一个没有 detail 的 500。这类失败不该清掉页面上已有的数据，只挂一条提示并继续轮询。
 */
export function isEngineUnreachable(e: unknown): boolean {
  if (e instanceof TypeError) return true;
  if (e instanceof ApiError) {
    if (e.status >= 502 && e.status <= 504) return true;
    if (e.status === 500 && (e.body === undefined || e.body === null || e.body === '')) return true;
  }
  return false;
}
