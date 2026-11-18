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
  /** 引擎建议的那一个操作：retry | edit_scope | broaden | add_papers。认不出的按 retry 处理。 */
  action?: string | null;
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
  /** 最多检索并筛选多少篇（5..500，默认 200） */
  max_papers?: number | null;
  /** 给相关度最高的多少篇写解读（默认 60）；界面不改它，原样带回 */
  compile_top_n?: number | null;
  /** 相关度达到多少才留下（默认 0.5，「放宽范围」时 0.35）；界面不改它，原样带回 */
  relevance_threshold?: number | null;
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
  created_at: string;
  updated_at: string;
}

/** 用量：token 数与按标价估的费用（美元，不计缓存折扣，是上限的近似）。 */
export interface ResearchUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd: number;
}

/** 一个文献来源这次拿到了多少篇；error 不为空 = 这个来源这次没连上。 */
export interface SurveySource {
  source: string;
  /** 这个来源返回了多少条 */
  fetched: number;
  /** 其中多少篇被取进来筛选（旧的检索方式不分，为空） */
  selected?: number | null;
  error: string | null;
}

/** 检索时向一个来源发的一次请求。 */
export interface SurveyQuery {
  source: string;
  query: string;
  /** relevance | date … */
  sort: string;
  /** 时间范围，如 2016..2026 */
  window: string;
  returned: number;
  error?: string | null;
}

export interface SurveyProgress {
  /** 各来源返回的条数之和（去重前）；旧引擎不带 */
  fetched?: number;
  /** 取进来筛选的候选（去重后） */
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
  /** 正在做哪一步：引擎写给日志的标题（只有中文）。界面用 step_action 出自己的说法 */
  step_title: string | null;
  /** 这一步的动作名，如 wiki.compile；任务没在跑时为 null */
  step_action?: string | null;
  started_at: string | null;
  /** 任务结束（完成 / 失败 / 取消）的时刻 */
  finished_at: string | null;
  progress: SurveyProgress;
  top_papers: SurveyPaper[];
  /** 引擎写的检索提示（哪个来源失败了等）。界面不直接显示这些句子，改用 sources 自己说 */
  notes: string[];
  /** 各来源这次拿到多少 */
  sources: SurveySource[];
  /** 这次检索发出的每一条查询；旧引擎不带 */
  queries?: SurveyQuery[];
  usage: ResearchUsage;
}

export interface ResearchHypotheses {
  voyage_id: string | null;
  state: string | null;
  step_title: string | null;
  step_action?: string | null;
  started_at: string | null;
  finished_at: string | null;
  /** 已完成几轮 */
  round: number;
  max_rounds: number;
  node_count: number;
  usage: ResearchUsage;
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
  source_voyage_id: string | null;
  created_at: string;
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
  /** 即将进行的是第几次自动重试（1..3；0 = 没有自动重试过） */
  retry_attempt: number;
  /** 当前阶段的任务停在出错状态时的原因 */
  last_error: ResearchLastError | null;
  /** 这次研究到目前为止的用量（各阶段加起来，含重做过的） */
  usage: ResearchUsage;
  capabilities: Capabilities;
}

/** 这台电脑现在能做什么：有没有登记过远程机器、能不能把稿件编译成 PDF。 */
export interface Capabilities {
  remote_machine: boolean;
  latex: boolean;
}

/** 开始之前的估计：一次研究通常要多久、花多少。数字都是范围。 */
export interface ResearchEstimate {
  minutes_low?: number | null;
  minutes_high?: number | null;
  cost_usd_low?: number | null;
  cost_usd_high?: number | null;
}

export interface CreateRunInput {
  /** 带 project_id 时可以不填：用课题的说明，没有就用课题名 */
  direction?: string;
  /** 在已有的课题上开始研究（不新建课题）。课题已有研究回 409 RESEARCH_ALREADY_EXISTS */
  project_id?: string;
  /** 幂等键：同一个键重复提交返回同一次研究 */
  client_key: string;
  brief?: ResearchBrief;
}

export interface RetryRunInput {
  /** 带上就按这份范围重新检索 */
  brief?: ResearchBrief;
  /** 放宽后重新检索：相关度门槛 0.5、时间拉到 5 年 */
  broaden?: boolean;
  /** 明确重做哪个阶段；不给 = 重试当前阶段 */
  phase?: 'survey' | 'hypotheses';
}

export const researchApi = {
  /** 只生成范围、不创建任何东西；一次模型调用，要等几秒。 */
  draftBrief(direction: string): Promise<ResearchBrief> {
    return requestJson<ResearchBrief>('/research/brief', 'POST', { direction });
  },
  createRun(input: CreateRunInput): Promise<ResearchRunRead> {
    return requestJson<ResearchRunRead>('/research', 'POST', input);
  },
  /** 不依赖任何一次研究：写作页、实验页打开时就要知道。 */
  getCapabilities(): Promise<Capabilities> {
    return request<Capabilities>('/capabilities');
  },
  /** 开始前的时间和费用估计。引擎还没有这个接口时会 404，调用方用固定的范围兜底。 */
  getEstimate(): Promise<ResearchEstimate> {
    return request<ResearchEstimate>('/research/estimate');
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
  /** 在同一个文献库上再找一批假设；rounds 不给用默认轮数。 */
  moreIdeas(id: string, rounds?: number): Promise<ResearchRunRead> {
    return requestJson<ResearchRunRead>(`/research/${id}/more-ideas`, 'POST', rounds ? { rounds } : {});
  },
  renameRun(id: string, name: string): Promise<ResearchRunRead> {
    return requestJson<ResearchRunRead>(`/research/${id}`, 'PATCH', { name });
  },
  /**
   * 删除这次研究。purge=false 只删研究记录：课题、文献库、论文和想法都留着；
   * purge=true 连课题和它的文献库一起删。
   */
  deleteRun(id: string, purge: boolean): Promise<void> {
    return request<void>(`/research/${id}${purge ? '?purge=true' : ''}`, { method: 'DELETE' });
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
