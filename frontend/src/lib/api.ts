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
    try {
      const body: unknown = await res.json();
      if (body && typeof body === 'object' && 'detail' in body) {
        const d = (body as { detail: unknown }).detail;
        detail = typeof d === 'string' ? d : JSON.stringify(d);
      }
    } catch {
      /* non-JSON error body — keep statusText */
    }
    throw new ApiError(res.status, detail);
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
}

export interface PaperConceptRef {
  id: string;
  name: string;
  category: ConceptCategory;
}

export interface PaperDetail extends PaperRead {
  abstract: string | null;
  /** markdown，双链为 [[概念名]] */
  wiki_content: string | null;
  pdf_available: boolean;
  concepts: PaperConceptRef[];
}

export interface PageOf<T> {
  items: T[];
  total: number;
  page: number;
  size: number;
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
    opts: { status?: PaperStatus; q?: string; sort?: PaperSort; page?: number; size?: number } = {},
  ): Promise<PageOf<PaperRead>> {
    const params = new URLSearchParams();
    if (opts.status) params.set('status', opts.status);
    if (opts.q) params.set('q', opts.q);
    if (opts.sort) params.set('sort', opts.sort);
    if (opts.page) params.set('page', String(opts.page));
    if (opts.size) params.set('size', String(opts.size));
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
  async downloadObsidianExport(projectId: string): Promise<Blob> {
    const headers = new Headers();
    const token = getToken();
    if (token) headers.set('Authorization', `Bearer ${token}`);
    const res = await fetch(`${BASE}/projects/${projectId}/export/obsidian`, { headers });
    if (!res.ok) throw new ApiError(res.status, res.statusText || `HTTP ${res.status}`);
    return res.blob();
  },

  // —— M2 · Dashboard 统计 ——
  getStats(projectId: string): Promise<StatsRead> {
    return request<StatsRead>(`/projects/${projectId}/stats`);
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
