/* ============================================================
   Polaris API client — thin fetch wrapper.
   baseURL /api (proxied to FastAPI at :8000 in dev), JSON,
   Bearer token from localStorage.
   Backend auth is fastapi-users:
     POST /api/auth/jwt/login    form-encoded username/password
     POST /api/auth/register     JSON
     GET  /api/users/me
   M1 契约见 docs/api-m1.md（Projects / Voyages / Gates / Admin LLM）。
   ============================================================ */

const BASE = '/api';
const TOKEN_KEY = 'polaris.token';

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
    /** 解析后的错误响应体（如 409 PAPER_EXISTS 时含 paper_id），可能为空 */
    public readonly body?: unknown,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  if (token) {
    localStorage.setItem(TOKEN_KEY, token);
  } else {
    localStorage.removeItem(TOKEN_KEY);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`);
  }
  const res = await fetch(`${BASE}${path}`, { ...init, headers });
  if (!res.ok) {
    let detail = res.statusText || `HTTP ${res.status}`;
    let body: unknown;
    try {
      body = await res.json();
      if (body && typeof body === 'object' && 'detail' in body) {
        const d = (body as { detail: unknown }).detail;
        detail = typeof d === 'string' ? d : JSON.stringify(d);
      }
    } catch {
      /* non-JSON error body — keep statusText */
    }
    throw new ApiError(res.status, detail, body);
  }
  if (res.status === 204) {
    return undefined as T;
  }
  return (await res.json()) as T;
}

function requestJson<T>(path: string, method: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

/** 二进制下载（PDF / zip / .bib 等），带 Bearer，错误时解析 detail。 */
async function requestBlob(path: string): Promise<Blob> {
  const headers = new Headers();
  const token = getToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const res = await fetch(`${BASE}${path}`, { headers });
  if (!res.ok) {
    let detail = res.statusText || `HTTP ${res.status}`;
    let body: unknown;
    try {
      body = await res.json();
      if (body && typeof body === 'object' && 'detail' in body) {
        const d = (body as { detail: unknown }).detail;
        detail = typeof d === 'string' ? d : JSON.stringify(d);
      }
    } catch {
      /* keep statusText */
    }
    throw new ApiError(res.status, detail, body);
  }
  return res.blob();
}

// ============================================================
// Users
// ============================================================

export interface UserRead {
  id: string;
  email: string;
  is_active: boolean;
  is_superuser: boolean;
  is_verified: boolean;
  /** Polaris 扩展字段（后端可能暂未返回，均可选） */
  display_name?: string | null;
  role?: string;
}

export interface RegisterInput {
  email: string;
  password: string;
  invite_code: string;
}

/** admin 判定：role=admin 或 fastapi-users superuser。 */
export function isAdmin(u: UserRead | undefined | null): boolean {
  return !!u && (u.role === 'admin' || u.is_superuser === true);
}

// ============================================================
// Projects（研究方向）— definition 为结构化访谈结果，允许部分草稿
// ============================================================

export interface RubricDimension {
  name: string;
  description: string;
  weight: number;
}

export interface AnchorPaper {
  title: string;
  arxiv_id?: string;
  url?: string;
  reason?: string;
}

export interface KeywordSpec {
  arxiv_categories?: string[];
  include?: string[];
  synonyms?: Record<string, string[]>;
}

export interface ProjectDefinition {
  statement?: string;
  goals?: string[];
  in_scope?: string[];
  out_of_scope?: string[];
  questions?: string[];
  rubric?: RubricDimension[];
  anchor_papers?: AnchorPaper[];
  keywords?: KeywordSpec;
  cadence?: string;
}

/** AI 补全高级设置 — POST /projects/draft-definition 入参。 */
export interface DraftDefinitionInput {
  statement: string;
  name: string;
  keywords_include: string[];
}

/** AI 补全结果；source=fallback 表示 LLM 未配置，后端用默认模板生成。 */
export interface DraftDefinitionResult {
  definition: ProjectDefinition;
  source: 'llm' | 'fallback';
}

export interface ProjectMemberRead {
  user_id?: string;
  email?: string;
  display_name?: string | null;
  role?: string;
}

export interface ProjectRead {
  id: string;
  name: string;
  definition: ProjectDefinition | null;
  status?: string;
  members?: ProjectMemberRead[];
  created_at?: string;
  updated_at?: string;
}

// ============================================================
// Voyages（长时程 agent 任务）
// ============================================================

export type VoyageStatus =
  | 'planning'
  | 'executing'
  | 'verifying'
  | 'replanning'
  | 'paused_gate'
  | 'paused_error'
  | 'done'
  | 'failed'
  | 'cancelled';

/** 终态集合（不再产生 SSE 事件）。 */
export const VOYAGE_TERMINAL: ReadonlySet<string> = new Set(['done', 'failed', 'cancelled']);

export interface VoyageVerdict {
  passed: boolean;
  reason: string;
}

export interface VoyageStepRead {
  id: string;
  seq: number;
  title: string;
  action: string;
  params: unknown;
  observation: unknown;
  verdict: VoyageVerdict | null;
  status: string;
  tokens: number | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface VoyageRead {
  id: string;
  kind: string;
  goal: string;
  status: VoyageStatus;
  plan: unknown;
  cursor: number | null;
  budget: Record<string, unknown> | null;
  usage: Record<string, unknown> | null;
  project_id: string;
  created_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface VoyageDetail extends VoyageRead {
  steps: VoyageStepRead[];
}

// ============================================================
// Gates（人在环闸门）
// ============================================================

export type GateDecision = 'approve' | 'reject';

export interface GateRead {
  id: string;
  kind: string;
  status: 'pending' | 'approved' | 'rejected';
  payload: Record<string, unknown> | null;
  project_id: string;
  requested_by: string | null;
  decided_by: string | null;
  comment: string | null;
  created_at: string;
  decided_at: string | null;
}

// ============================================================
// Admin · LLM
// ============================================================

export type LlmProviderKind = 'openai_compat' | 'anthropic' | 'fake';

export const LLM_STAGES = [
  'default',
  'navigator',
  'sextant',
  'interview',
  'relevance',
  'librarian',
  'reading',
  'embedding',
  'forge',
  'debate',
  'experiment',
  'writing',
  'review',
] as const;

export type LlmStage = (typeof LLM_STAGES)[number];

export interface LlmProviderRead {
  id: string;
  name: string;
  kind: LlmProviderKind;
  base_url: string | null;
  api_key_masked: string | null;
  enabled: boolean;
}

export interface LlmProviderInput {
  name: string;
  kind: LlmProviderKind;
  base_url?: string;
  /** 空字符串 = 不变（PATCH 时） */
  api_key?: string;
  enabled: boolean;
}

export interface LlmRoute {
  stage: string;
  provider_id: string;
  model: string;
  temperature?: number | null;
}

export interface LlmUsageRow {
  date: string;
  stage: string;
  model: string;
  prompt_tokens: number;
  completion_tokens: number;
  calls: number;
}

// ============================================================
// M2 · Papers（论文库）— docs/api-m2.md
// ============================================================

export type PaperStatus = 'candidate' | 'scored' | 'excluded' | 'fetched' | 'compiled' | 'included';

export type PaperSort = 'relevance' | '-published_at';

export interface PaperAuthor {
  name: string;
}

export interface PaperRead {
  id: string;
  project_id: string;
  title: string;
  authors: PaperAuthor[];
  year: number | null;
  venue: string | null;
  arxiv_id: string | null;
  doi: string | null;
  url: string | null;
  published_at: string | null;
  /** 0-1，未打分为 null */
  relevance_score: number | null;
  status: PaperStatus;
  tldr: string | null;
  has_wiki: boolean;
  created_at: string;
  /* —— 文献管理增强字段（docs/api-lit.md §5，后端未就绪时可能缺失，均可选容错） —— */
  /** 项目级标签 */
  tags?: string[];
  /** 当前用户是否星标 */
  starred?: boolean;
  /** 当前用户阅读状态（无记录默认 unread） */
  reading_status?: ReadingStatus;
  /** 该论文笔记条数 */
  note_count?: number;
}

export interface PaperConceptRef {
  id: string;
  name: string;
  category: ConceptCategory;
}

/** 论文图片元数据（docs/api-lit.md §6.5）；文件本体走 fetchFigureImage blob。 */
export interface FigureInfo {
  index: number;
  page: number;
  width: number;
  height: number;
  /** 视觉模型生成的一句话中文说明；降级提取时为 null */
  caption: string | null;
  /** 视觉模型判定的重要图 */
  important: boolean;
}

export interface PaperDetail extends PaperRead {
  abstract: string | null;
  /** markdown，双链为 [[概念名]] */
  wiki_content: string | null;
  pdf_available: boolean;
  concepts: PaperConceptRef[];
  /** 论文图片列表（后端未就绪时可能缺失） */
  figures?: FigureInfo[];
}

export interface PageOf<T> {
  items: T[];
  total: number;
  page: number;
  size: number;
}

// ============================================================
// Lit · 阅读 / 笔记 / 标签 / 引用导出 — docs/api-lit.md
// ============================================================

export type ReadingStatus = 'unread' | 'reading' | 'read';

export interface NoteRead {
  id: string;
  paper_id: string;
  project_id: string;
  author_id: string;
  /** display_name 回退 email 前缀 */
  author_name: string;
  content: string;
  created_at: string;
  updated_at: string;
}

export interface NoteWithPaper extends NoteRead {
  paper_title: string;
}

/** 手动添加文献：三选一。 */
export type PaperImportInput = { arxiv_id: string } | { doi: string } | { bibtex: string };

export interface TagRead {
  id: string;
  name: string;
  paper_count: number;
}

export interface MyMeta {
  starred: boolean;
  reading_status: ReadingStatus;
}

export type CitationFormat = 'bibtex' | 'csl-json';

/** AI 伴读多轮历史消息（前端无状态携带，最多最近 10 轮）。 */
export interface ChatTurn {
  role: 'user' | 'assistant';
  content: string;
}

// ============================================================
// M2 · Concepts（概念库）
// ============================================================

export type ConceptCategory =
  | 'method'
  | 'architecture'
  | 'methodology'
  | 'problem'
  | 'metric'
  | 'dataset'
  | 'other';

export interface ConceptRead {
  id: string;
  project_id: string;
  name: string;
  category: ConceptCategory;
  /** 一句话定义 */
  definition: string | null;
  paper_count: number;
}

export interface ConceptPaperRef {
  id: string;
  title: string;
  year: number | null;
}

export interface ConceptDetail extends ConceptRead {
  wiki_content: string | null;
  papers: ConceptPaperRef[];
  related: { id: string; name: string }[];
}

// ============================================================
// M2 · Search（关键词 / 语义检索）
// ============================================================

export type SearchMode = 'keyword' | 'semantic';

export interface SearchResult {
  papers: (PaperRead & { score?: number | null })[];
  concepts: (ConceptRead & { score?: number | null })[];
  /** semantic 不可用时后端回退 keyword，并在此说明实际使用的模式 */
  mode_used?: SearchMode;
}

// ============================================================
// M2 · Ingest（冷启动 / 增量同步，复用 Voyage）
// ============================================================

export type IngestMode = 'bootstrap' | 'incremental';

export interface IngestKnobs {
  /** bootstrap 回填月数（3-24） */
  months_back?: number;
  /** 本次最多精读编译篇数（成本上限） */
  max_papers?: number;
  /** 相关度阈值 0-1 */
  relevance_threshold?: number;
  /** 引文雪球层数 0-2 */
  snowball_depth?: number;
  /** 打分后精读编译前 N 篇 */
  compile_top_n?: number;
}

export interface IngestLastRun {
  voyage_id: string;
  status: string;
  finished_at: string | null;
}

export interface PaperCounts {
  candidate?: number;
  scored?: number;
  fetched?: number;
  compiled?: number;
  excluded?: number;
  included?: number;
  total?: number;
}

export interface IngestState {
  /** 水位线日期（ISO）；从未 ingest 过为 null */
  watermark: string | null;
  last_run: IngestLastRun | null;
  paper_counts: PaperCounts;
  running_voyage_id: string | null;
}

// ============================================================
// M2 · Dashboard 统计
// ============================================================

export interface ActivityRead {
  id: string;
  kind: string;
  message: string;
  created_at: string;
}

export interface StatsRead {
  papers_total: number;
  papers_today: number;
  ideas_candidate: number;
  gates_pending: number;
  recent_activities: ActivityRead[];
}

// ============================================================
// M3 · Ideas（Idea Forge 候选池）— docs/api-m3.md
// ============================================================

export type IdeaStatus = 'candidate' | 'under_review' | 'promoted' | 'rejected';

export type IdeaSort = 'elo' | '-created_at' | 'score';

/** 四维评分（0-10）。 */
export interface IdeaScores {
  novelty: number;
  feasibility: number;
  operability: number;
  impact: number;
}

export interface IdeaRead {
  id: string;
  project_id: string;
  title: string;
  summary: string;
  /** 未打分为 null */
  scores: IdeaScores | null;
  elo_rating: number;
  status: IdeaStatus;
  created_at: string;
}

export interface IdeaParentPaper {
  id: string;
  title: string;
}

export interface IdeaDetail extends IdeaRead {
  /** markdown：动机/方法概述/预期实验/风险 */
  content: string;
  parent_paper_ids: string[];
  parent_papers: IdeaParentPaper[];
  score_rationale: Partial<Record<keyof IdeaScores, string>> | null;
}

// ============================================================
// M3 · Forge（idea 生成 voyage）
// ============================================================

export interface ForgeKnobs {
  num_ideas?: number;
  dedup_threshold?: number;
  max_context_papers?: number;
}

export interface IdeaCounts {
  candidate?: number;
  under_review?: number;
  promoted?: number;
  rejected?: number;
  total?: number;
}

export interface ForgeLastRun {
  voyage_id?: string;
  status?: string;
  finished_at?: string | null;
}

export interface ForgeState {
  /** 进行中的 forge/review voyage（同项目同时只允许一个）。 */
  running_voyage_id: string | null;
  last_run: ForgeLastRun | null;
  idea_counts: IdeaCounts;
}

// ============================================================
// M3 · Review（多 agent 辩论锦标赛 + Elo + 人机讨论）
// ============================================================

export interface ReviewPersona {
  name: string;
  stance: string;
}

export interface TournamentInput {
  /** null = 全部 candidate/under_review */
  idea_ids?: string[] | null;
  /** 每对 idea 的辩论轮数 */
  rounds?: number;
  /** null = 默认三人设 */
  personas?: ReviewPersona[] | null;
}

export interface LeaderboardRow extends IdeaRead {
  matches: number;
  wins: number;
}

/** idea_match = 一场辩论；idea_discussion = 常驻人机讨论区。 */
export type ReviewTargetType = 'idea_match' | 'idea_discussion';

export interface ReviewSessionRead {
  id: string;
  target_type: string;
  target_id: string;
  status: string;
  /** idea_match 时含 idea_a / idea_b / winner */
  payload: Record<string, unknown> | null;
  created_at: string;
}

export interface ReviewMessageRead {
  id: string;
  session_id: string;
  author_type: 'agent' | 'human';
  /** 人设名或用户 display name */
  author_name: string;
  content: string;
  round: number | null;
  created_at: string;
}

// ============================================================
// M4 · SSH 凭据（每用户私有）— docs/api-m4.md §1
// ============================================================

export interface SshCredentialRead {
  id: string;
  name: string;
  host: string;
  port: number;
  username: string;
  created_at: string;
  /** 最近一次测试连接成功时间；从未验证为 null */
  last_verified_at: string | null;
}

export interface SshCredentialInput {
  name: string;
  host: string;
  port?: number;
  username: string;
  /** PEM 文本，后端 Fernet 加密入库，绝不回传 */
  private_key: string;
  passphrase?: string;
}

export interface SshTestResult {
  ok: boolean;
  detail: string;
}

// ============================================================
// M4 · Experiments（实验，与 kind=experiment 的 voyage 1:1）
// ============================================================

export type ExperimentStatus =
  | 'planning'
  | 'awaiting_gate'
  | 'setup'
  | 'running'
  | 'reporting'
  | 'done'
  | 'failed'
  | 'cancelled';

/** 实验终态集合。 */
export const EXPERIMENT_TERMINAL: ReadonlySet<string> = new Set(['done', 'failed', 'cancelled']);

export interface ExperimentBudget {
  max_hours?: number;
  max_runs?: number;
}

export interface ExperimentRead {
  id: string;
  project_id: string;
  idea_id: string;
  idea_title: string;
  status: ExperimentStatus;
  voyage_id: string | null;
  workdir: string | null;
  server_host: string | null;
  budget: ExperimentBudget | null;
  created_at: string;
  updated_at: string;
}

export type HypothesisStatus = 'testing' | 'verified' | 'falsified';

export interface ExperimentHypothesis {
  text: string;
  status: HypothesisStatus;
}

/** plan JSON（契约只列字段名，内层结构宽松处理）。 */
export interface ExperimentPlan {
  hypotheses?: ExperimentHypothesis[];
  repro_strategy?: string;
  steps?: (string | { title?: string; desc?: string; description?: string })[];
  budget_estimate?: string | Record<string, unknown>;
}

export type ExperimentRunStatus = 'running' | 'succeeded' | 'failed';

export interface ExperimentRunRead {
  id: string;
  seq: number;
  command: string;
  status: ExperimentRunStatus;
  exit_code: number | null;
  log_path: string | null;
  metrics: Record<string, unknown> | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface ExperimentMetricPoint {
  step: number;
  value: number;
}

export interface ExperimentDetail extends ExperimentRead {
  plan: ExperimentPlan | null;
  runs: ExperimentRunRead[];
  /** markdown 报告，未生成为 null */
  report: string | null;
  /** {指标名: [{step, value}]} */
  metrics: Record<string, ExperimentMetricPoint[]> | null;
}

export interface ExperimentLogs {
  lines: string[];
  truncated: boolean;
}

export interface CreateExperimentInput {
  idea_id: string;
  credential_id: string;
  params?: {
    gpu_hint?: string;
    budget?: ExperimentBudget;
  };
}

// ============================================================
// api object
// ============================================================

export const api = {
  /** fastapi-users JWT login — form-encoded username/password. Returns access token. */
  async login(email: string, password: string): Promise<string> {
    const body = new URLSearchParams({ username: email, password });
    const data = await request<{ access_token: string; token_type: string }>('/auth/jwt/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body,
    });
    return data.access_token;
  },

  /** fastapi-users register — JSON body, invite_code is a Polaris extension. */
  register(input: RegisterInput): Promise<UserRead> {
    return requestJson<UserRead>('/auth/register', 'POST', input);
  },

  /** Current user. */
  me(): Promise<UserRead> {
    return request<UserRead>('/users/me');
  },

  // —— Projects ——
  listProjects(): Promise<ProjectRead[]> {
    return request<ProjectRead[]>('/projects');
  },
  createProject(input: { name: string; definition: ProjectDefinition }): Promise<ProjectRead> {
    return requestJson<ProjectRead>('/projects', 'POST', input);
  },
  /** 由 LLM 根据一句话定义草拟完整 definition（目标/问题/rubric/同义词等）。 */
  draftDefinition(input: DraftDefinitionInput): Promise<DraftDefinitionResult> {
    return requestJson<DraftDefinitionResult>('/projects/draft-definition', 'POST', input);
  },
  getProject(id: string): Promise<ProjectRead> {
    return request<ProjectRead>(`/projects/${id}`);
  },
  patchProject(
    id: string,
    input: { name?: string; definition?: ProjectDefinition; status?: string },
  ): Promise<ProjectRead> {
    return requestJson<ProjectRead>(`/projects/${id}`, 'PATCH', input);
  },
  addProjectMember(id: string, input: { email: string; role: 'member' | 'owner' }): Promise<void> {
    return requestJson<void>(`/projects/${id}/members`, 'POST', input);
  },

  // —— Voyages ——
  createVoyage(input: {
    kind: string;
    project_id: string;
    goal: string;
    params?: Record<string, unknown>;
  }): Promise<VoyageRead> {
    return requestJson<VoyageRead>('/voyages', 'POST', input);
  },
  listVoyages(projectId?: string): Promise<VoyageRead[]> {
    const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
    return request<VoyageRead[]>(`/voyages${qs}`);
  },
  getVoyage(id: string): Promise<VoyageDetail> {
    return request<VoyageDetail>(`/voyages/${id}`);
  },
  cancelVoyage(id: string): Promise<VoyageRead> {
    return request<VoyageRead>(`/voyages/${id}/cancel`, { method: 'POST' });
  },
  /** 重试 paused_error 的航程，从断点续跑。 */
  resumeVoyage(id: string): Promise<VoyageRead> {
    return request<VoyageRead>(`/voyages/${id}/resume`, { method: 'POST' });
  },

  // —— Gates ——
  listGates(status?: 'pending' | 'decided', projectId?: string): Promise<GateRead[]> {
    const params = new URLSearchParams();
    if (status) params.set('status', status);
    if (projectId) params.set('project_id', projectId);
    const qs = params.toString();
    return request<GateRead[]>(`/gates${qs ? `?${qs}` : ''}`);
  },
  decideGate(id: string, decision: GateDecision, comment?: string): Promise<GateRead> {
    return requestJson<GateRead>(`/gates/${id}/${decision}`, 'POST', comment ? { comment } : {});
  },

  // —— M2 · Papers ——
  listPapers(
    projectId: string,
    opts: {
      status?: PaperStatus;
      q?: string;
      sort?: PaperSort;
      page?: number;
      size?: number;
      /** 按标签名过滤 */
      tag?: string;
      /** 仅星标 */
      starred?: boolean;
      /** 按当前用户阅读状态过滤 */
      reading_status?: ReadingStatus;
    } = {},
  ): Promise<PageOf<PaperRead>> {
    const params = new URLSearchParams();
    if (opts.status) params.set('status', opts.status);
    if (opts.q) params.set('q', opts.q);
    if (opts.sort) params.set('sort', opts.sort);
    if (opts.page) params.set('page', String(opts.page));
    if (opts.size) params.set('size', String(opts.size));
    if (opts.tag) params.set('tag', opts.tag);
    if (opts.starred) params.set('starred', 'true');
    if (opts.reading_status) params.set('reading_status', opts.reading_status);
    const qs = params.toString();
    return request<PageOf<PaperRead>>(`/projects/${projectId}/papers${qs ? `?${qs}` : ''}`);
  },
  getPaper(id: string): Promise<PaperDetail> {
    return request<PaperDetail>(`/papers/${id}`);
  },
  /** 人工纳入/排除。 */
  patchPaper(id: string, input: { status: 'included' | 'excluded' }): Promise<PaperRead> {
    return requestJson<PaperRead>(`/papers/${id}`, 'PATCH', input);
  },

  // —— Lit · PDF 阅读 ——
  /** 论文 PDF 原件（blob，用 objectURL 喂给 iframe）。无 PDF → 404 PDF_NOT_AVAILABLE。 */
  fetchPaperPdf(id: string): Promise<Blob> {
    return requestBlob(`/papers/${id}/pdf`);
  },
  /** 按需补下 PDF（仅 arXiv 来源）；已有 PDF 时幂等直接返回。 */
  requestPaperPdf(id: string): Promise<PaperDetail> {
    return request<PaperDetail>(`/papers/${id}/fetch-pdf`, { method: 'POST' });
  },

  // —— Lit · 论文图片（docs/api-lit.md §6.5） ——
  /** 论文图片元数据列表；无图返回 []。 */
  listFigures(id: string): Promise<FigureInfo[]> {
    return request<FigureInfo[]>(`/papers/${id}/figures`);
  },
  /** 单张图片 PNG（blob → objectURL 显示）。 */
  fetchFigureImage(id: string, index: number): Promise<Blob> {
    return requestBlob(`/papers/${id}/figures/${index}/image`);
  },
  /** 从 PDF 提取图片并由视觉模型筛选重要图；已有 figures 且非 force 时幂等直返。无 PDF → 404 PDF_NOT_AVAILABLE。 */
  extractFigures(id: string, force = false): Promise<{ figures: FigureInfo[] }> {
    return request<{ figures: FigureInfo[] }>(
      `/papers/${id}/extract-figures${force ? '?force=true' : ''}`,
      { method: 'POST' },
    );
  },

  // —— Lit · 笔记 ——
  listPaperNotes(paperId: string): Promise<NoteRead[]> {
    return request<NoteRead[]>(`/papers/${paperId}/notes`);
  },
  createPaperNote(paperId: string, content: string): Promise<NoteRead> {
    return requestJson<NoteRead>(`/papers/${paperId}/notes`, 'POST', { content });
  },
  patchNote(noteId: string, content: string): Promise<NoteRead> {
    return requestJson<NoteRead>(`/notes/${noteId}`, 'PATCH', { content });
  },
  deleteNote(noteId: string): Promise<void> {
    return request<void>(`/notes/${noteId}`, { method: 'DELETE' });
  },
  /** 项目笔记本：全项目笔记分页 + 搜索。 */
  listProjectNotes(
    projectId: string,
    opts: { q?: string; paper_id?: string; page?: number; size?: number } = {},
  ): Promise<PageOf<NoteWithPaper>> {
    const params = new URLSearchParams();
    if (opts.q) params.set('q', opts.q);
    if (opts.paper_id) params.set('paper_id', opts.paper_id);
    if (opts.page) params.set('page', String(opts.page));
    if (opts.size) params.set('size', String(opts.size));
    const qs = params.toString();
    return request<PageOf<NoteWithPaper>>(`/projects/${projectId}/notes${qs ? `?${qs}` : ''}`);
  },

  // —— Lit · 手动添加文献 ——
  /** 409 → ApiError(detail=PAPER_EXISTS, body 含 paper_id)；422 → PARSE_FAILED。 */
  importPaper(projectId: string, input: PaperImportInput): Promise<PaperDetail> {
    return requestJson<PaperDetail>(`/projects/${projectId}/papers`, 'POST', input);
  },

  // —— Lit · 标签与个人状态 ——
  listTags(projectId: string): Promise<TagRead[]> {
    return request<TagRead[]>(`/projects/${projectId}/tags`);
  },
  /** 整组覆盖论文标签；新名字自动建 tag；空数组=清空。 */
  putPaperTags(id: string, names: string[]): Promise<PaperDetail> {
    return requestJson<PaperDetail>(`/papers/${id}/tags`, 'PUT', { names });
  },
  /** 个人星标 / 阅读状态。 */
  putMyMeta(id: string, input: Partial<MyMeta>): Promise<MyMeta> {
    return requestJson<MyMeta>(`/papers/${id}/my-meta`, 'PUT', input);
  },

  // —— Lit · 引用导出（.bib / CSL-JSON blob） ——
  downloadCitations(
    projectId: string,
    opts: { format: CitationFormat; status?: PaperStatus; tag?: string; starred?: boolean },
  ): Promise<Blob> {
    const params = new URLSearchParams({ format: opts.format });
    if (opts.status) params.set('status', opts.status);
    if (opts.tag) params.set('tag', opts.tag);
    if (opts.starred) params.set('starred', 'true');
    return requestBlob(`/projects/${projectId}/export/citations?${params.toString()}`);
  },

  // —— M2 · Concepts ——
  listConcepts(
    projectId: string,
    opts: { category?: ConceptCategory; q?: string } = {},
  ): Promise<ConceptRead[]> {
    const params = new URLSearchParams();
    if (opts.category) params.set('category', opts.category);
    if (opts.q) params.set('q', opts.q);
    const qs = params.toString();
    return request<ConceptRead[]>(`/projects/${projectId}/concepts${qs ? `?${qs}` : ''}`);
  },
  getConcept(id: string): Promise<ConceptDetail> {
    return request<ConceptDetail>(`/concepts/${id}`);
  },

  // —— M2 · Search ——
  searchProject(
    projectId: string,
    opts: { q: string; mode?: SearchMode; limit?: number },
  ): Promise<SearchResult> {
    const params = new URLSearchParams({ q: opts.q });
    if (opts.mode) params.set('mode', opts.mode);
    if (opts.limit) params.set('limit', String(opts.limit));
    return request<SearchResult>(`/projects/${projectId}/search?${params.toString()}`);
  },

  // —— M2 · Ingest ——
  startIngest(projectId: string, input: { mode: IngestMode; knobs?: IngestKnobs }): Promise<VoyageRead> {
    return requestJson<VoyageRead>(`/projects/${projectId}/ingest`, 'POST', input);
  },
  getIngestState(projectId: string): Promise<IngestState> {
    return request<IngestState>(`/projects/${projectId}/ingest/state`);
  },

  // —— M2 · Obsidian 导出（zip blob） ——
  downloadObsidianExport(projectId: string): Promise<Blob> {
    return requestBlob(`/projects/${projectId}/export/obsidian`);
  },

  // —— M2 · Dashboard 统计 ——
  getStats(projectId: string): Promise<StatsRead> {
    return request<StatsRead>(`/projects/${projectId}/stats`);
  },

  // —— M3 · Forge ——
  startForge(projectId: string, knobs: ForgeKnobs): Promise<VoyageRead> {
    return requestJson<VoyageRead>(`/projects/${projectId}/forge`, 'POST', { knobs });
  },
  getForgeState(projectId: string): Promise<ForgeState> {
    return request<ForgeState>(`/projects/${projectId}/forge/state`);
  },

  // —— M3 · Ideas ——
  listIdeas(projectId: string, opts: { status?: IdeaStatus; sort?: IdeaSort } = {}): Promise<IdeaRead[]> {
    const params = new URLSearchParams();
    if (opts.status) params.set('status', opts.status);
    if (opts.sort) params.set('sort', opts.sort);
    const qs = params.toString();
    return request<IdeaRead[]>(`/projects/${projectId}/ideas${qs ? `?${qs}` : ''}`);
  },
  getIdea(id: string): Promise<IdeaDetail> {
    return request<IdeaDetail>(`/ideas/${id}`);
  },
  /** 人工淘汰（其他状态转换走专用接口）。 */
  patchIdea(id: string, input: { status: 'rejected' }): Promise<IdeaRead> {
    return requestJson<IdeaRead>(`/ideas/${id}`, 'PATCH', input);
  },
  /** 发起晋级 → 创建 idea_promotion Gate（pending）。 */
  promoteIdea(id: string): Promise<GateRead> {
    return request<GateRead>(`/ideas/${id}/promote`, { method: 'POST' });
  },

  // —— M3 · Review 锦标赛 / 排行榜 ——
  startTournament(projectId: string, input: TournamentInput): Promise<VoyageRead> {
    return requestJson<VoyageRead>(`/projects/${projectId}/review/tournament`, 'POST', input);
  },
  getLeaderboard(projectId: string): Promise<LeaderboardRow[]> {
    return request<LeaderboardRow[]>(`/projects/${projectId}/review/leaderboard`);
  },

  // —— M3 · 讨论 / 辩论 session ——
  listIdeaSessions(ideaId: string): Promise<ReviewSessionRead[]> {
    return request<ReviewSessionRead[]>(`/ideas/${ideaId}/sessions`);
  },
  listSessionMessages(sessionId: string): Promise<ReviewMessageRead[]> {
    return request<ReviewMessageRead[]>(`/sessions/${sessionId}/messages`);
  },
  postSessionMessage(sessionId: string, content: string): Promise<ReviewMessageRead> {
    return requestJson<ReviewMessageRead>(`/sessions/${sessionId}/messages`, 'POST', { content });
  },

  // —— M4 · SSH 凭据 ——
  listSshCredentials(): Promise<SshCredentialRead[]> {
    return request<SshCredentialRead[]>('/ssh-credentials');
  },
  createSshCredential(input: SshCredentialInput): Promise<SshCredentialRead> {
    return requestJson<SshCredentialRead>('/ssh-credentials', 'POST', input);
  },
  deleteSshCredential(id: string): Promise<void> {
    return request<void>(`/ssh-credentials/${id}`, { method: 'DELETE' });
  },
  /** asyncssh 真连一次 + echo ok；成功则后端更新 last_verified_at。 */
  testSshCredential(id: string): Promise<SshTestResult> {
    return request<SshTestResult>(`/ssh-credentials/${id}/test`, { method: 'POST' });
  },

  // —— M4 · Experiments ——
  createExperiment(projectId: string, input: CreateExperimentInput): Promise<ExperimentRead> {
    return requestJson<ExperimentRead>(`/projects/${projectId}/experiments`, 'POST', input);
  },
  listExperiments(projectId: string): Promise<ExperimentRead[]> {
    return request<ExperimentRead[]>(`/projects/${projectId}/experiments`);
  },
  getExperiment(id: string): Promise<ExperimentDetail> {
    return request<ExperimentDetail>(`/experiments/${id}`);
  },
  /** 取消关联 voyage + 尝试 SSH kill 运行中的进程。 */
  cancelExperiment(id: string): Promise<void> {
    return request<void>(`/experiments/${id}/cancel`, { method: 'POST' });
  },
  getExperimentLogs(id: string, opts: { runId?: string; tail?: number } = {}): Promise<ExperimentLogs> {
    const params = new URLSearchParams();
    if (opts.runId) params.set('run_id', opts.runId);
    if (opts.tail) params.set('tail', String(opts.tail));
    const qs = params.toString();
    return request<ExperimentLogs>(`/experiments/${id}/logs${qs ? `?${qs}` : ''}`);
  },

  // —— Admin · LLM ——
  listLlmProviders(): Promise<LlmProviderRead[]> {
    return request<LlmProviderRead[]>('/admin/llm/providers');
  },
  createLlmProvider(input: LlmProviderInput): Promise<LlmProviderRead> {
    return requestJson<LlmProviderRead>('/admin/llm/providers', 'POST', input);
  },
  patchLlmProvider(id: string, input: Partial<LlmProviderInput>): Promise<LlmProviderRead> {
    return requestJson<LlmProviderRead>(`/admin/llm/providers/${id}`, 'PATCH', input);
  },
  deleteLlmProvider(id: string): Promise<void> {
    return request<void>(`/admin/llm/providers/${id}`, { method: 'DELETE' });
  },
  getLlmRoutes(): Promise<LlmRoute[]> {
    return request<LlmRoute[]>('/admin/llm/routes');
  },
  putLlmRoutes(routes: LlmRoute[]): Promise<LlmRoute[]> {
    return requestJson<LlmRoute[]>('/admin/llm/routes', 'PUT', routes);
  },
  getLlmUsage(opts: { projectId?: string; userId?: string; days?: number } = {}): Promise<LlmUsageRow[]> {
    const params = new URLSearchParams();
    if (opts.projectId) params.set('project_id', opts.projectId);
    if (opts.userId) params.set('user_id', opts.userId);
    if (opts.days) params.set('days', String(opts.days));
    const qs = params.toString();
    return request<LlmUsageRow[]>(`/admin/llm/usage${qs ? `?${qs}` : ''}`);
  },
};
