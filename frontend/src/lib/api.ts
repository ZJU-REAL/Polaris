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
  llm_access?: 'full' | 'chat_only' | 'blocked';
  has_avatar?: boolean;
  token_quota?: number | null;
  features?: Record<string, boolean> | null;
}

export interface UsageSummary {
  tokens_used: number;
  token_quota: number | null;
}

export interface AdminUserRead {
  id: string;
  email: string;
  display_name: string;
  role: string;
  is_active: boolean;
  has_avatar: boolean;
  llm_access: string;
  token_quota: number | null;
  features: Record<string, boolean> | null;
  tokens_used: number;
  created_at: string;
}

export interface InviteRead {
  id: string;
  project_id: string;
  token: string;
  expires_at: string | null;
  max_uses: number | null;
  used_count: number;
  revoked: boolean;
  created_at: string;
}

export interface InviteInfo {
  project_id: string;
  project_name: string;
  inviter_name: string | null;
  valid: boolean;
  already_member: boolean;
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
  /** 后端为 {prompt_tokens, completion_tokens} 字典（历史数据可能是数字） */
  tokens: { prompt_tokens?: number; completion_tokens?: number } | number | null;
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
  /** 本次任务快照使用的技能（启动时固定，见 docs/skill-system.md §3.2）。 */
  skills?: { slug: string; name: string; kind: string; version: number; target: string }[];
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
  'forge_signal',
  'goal_explore',
  'proposal',
  'proposal_review',
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

/** 状态组别名（docs/api-lit.md §8.5）：library=库内（达标及之后）；pending_compile=待编译。 */
export type PaperStatusFilter = PaperStatus | 'library' | 'pending_compile' | 'compiled_any';

export type PaperSort = 'relevance' | '-published_at';

export interface PaperAuthor {
  name: string;
}

export interface PaperRead {
  id: string;
  project_id: string;
  title: string;
  authors: PaperAuthor[];
  /** 发表机构（OpenAlex 补充；可能为空） */
  affiliations?: string[];
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
  /** 入库时间 */
  created_at: string;
  /** wiki 编译时间；未编译为 null（旧后端可能缺失） */
  compiled_at?: string | null;
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

/** 论文图片类型（视觉模型判定；编译时决定插到哪个小节）。 */
export type FigureKind = 'motivation' | 'method' | 'architecture' | 'experiment' | 'other';

/** 论文图片元数据（docs/api-lit.md §6.5）；文件本体走 fetchFigureImage blob。 */
export interface FigureInfo {
  index: number;
  page: number;
  width: number;
  height: number;
  /** 视觉模型生成的中文说明；降级提取时为 null */
  caption: string | null;
  /** 图片类型；旧数据/未注释为 null（后端未升级时可能缺失） */
  kind?: FigureKind | null;
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
// 文献知识底座：全文分段索引 + 文献库对话（docs/api-lit.md §8）
// ============================================================

/** 文献库对话的引用来源（SSE sources 事件 items）。 */
export interface LibraryChatSource {
  index: number;
  paper_id: string;
  title: string;
  year: number | null;
  status?: string | null;
  /** 0-1 相关度 */
  relevance?: number | null;
  /** 该论文关联的概念名（回答里的 [[双链]] 用） */
  concepts?: string[];
}

export interface RebuildIndexResult {
  papers_indexed: number;
  chunks_created: number;
  embedded: number;
  embed_error: string | null;
  total_chunks: number;
}

// ============================================================
// 知识图谱（论文 / 作者 / 概念网络）
// ============================================================

export type GraphNodeType = 'paper' | 'concept' | 'author';

export interface GraphNode {
  /** paper/concept 为 uuid，author 为 "author:<slug>" */
  id: string;
  type: GraphNodeType;
  label: string;
  status?: string | null;
  year?: number | null;
  /** 发表日期（ISO date），时间线按月分组用 */
  published?: string | null;
  relevance?: number | null;
  category?: string | null;
  /** author/concept 关联论文数（决定节点大小） */
  count?: number;
}

export interface GraphEdge {
  source: string;
  target: string;
  kind: 'paper_concept' | 'paper_author';
}

export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
  paper_total: number;
  truncated: boolean;
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
  /** 库内 = 相关性达标及之后（论文库计数口径） */
  library?: number;
  /** 待编译 = 达标但还没有 AI 解读 */
  pending_compile?: number;
}

export interface IngestState {
  /** 水位线日期（ISO）；从未 ingest 过为 null */
  watermark: string | null;
  last_run: IngestLastRun | null;
  paper_counts: PaperCounts;
  running_voyage_id: string | null;
  /** 下一次自动同步时间（ISO）；cadence 非 daily 或未完成初始建库为 null */
  next_sync_at?: string | null;
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
  ideas_under_review: number;
  experiments_active: number;
  experiments_running: number;
  manuscripts_total: number;
  manuscripts_under_review: number;
  gates_pending: number;
  recent_activities: ActivityRead[];
}

// ============================================================
// M3 · Ideas（Idea Forge 候选池）— docs/api-m3.md
// ============================================================

export type IdeaStatus = 'candidate' | 'under_review' | 'promoted' | 'rejected';

export type IdeaSort = 'elo' | '-created_at' | 'score';

/** idea 深度：sketch=方向草案（阶段 0 发散产物）| proposal=完整研究方案（深度生成产物）。 */
export type IdeaDepth = 'sketch' | 'proposal';

/** 研究类型枚举（docs/api-idea2.md §3 goal.research_type）。 */
export const RESEARCH_TYPES = ['method', 'benchmark', 'analysis', 'survey', 'application', 'theory'] as const;

export type ResearchType = (typeof RESEARCH_TYPES)[number];

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
  /** 草案 / 研究方案 */
  depth: IdeaDepth;
  /** 研究类型（method/benchmark/…）；草案通常为 null */
  research_type: string | null;
  created_at: string;
}

export interface IdeaParentPaper {
  id: string;
  title: string;
}

// —— Idea 2.0 · 研究目标与依据文献（docs/api-idea2.md §3/§7） ——

export interface IdeaGoalScope {
  in_scope?: string[];
  out_of_scope?: string[];
}

export interface IdeaGoalGrounding {
  paper_id: string;
  /** 该文献与目标的关系（支撑/空白/对比） */
  why: string;
}

export interface IdeaGoalResources {
  /** 算力需求描述 */
  compute?: string | null;
  /** 数据集名（含是否公开可得） */
  data?: string[];
  time_weeks?: number | null;
}

/** 研究目标（goal.explore 产物，随 Idea 落库）。字段均可选容错。 */
export interface IdeaGoal {
  research_type?: string;
  /** 研究任务（领域内的具体任务） */
  task?: string;
  /** 核心研究问题（一句话） */
  question?: string;
  /** 具体、可检验的研究目标，1-5 条 */
  objectives?: string[];
  scope?: IdeaGoalScope | null;
  /** 怎样算成功（可量化优先） */
  success_criteria?: string[];
  grounding?: IdeaGoalGrounding[];
  key_concepts?: string[];
  resources_needed?: IdeaGoalResources | null;
  /** 最小验证实验（1-3 天可出信号的 smoke 设计，结构化 JSON） */
  smoke_plan?: Record<string, unknown> | null;
}

export type IdeaEvidenceSource = 'library' | 'external' | 'signal';

/** 依据文献 / 证据条目。 */
export interface IdeaEvidence {
  /** 库内论文 id；外部文献 / 信号为 null */
  paper_id: string | null;
  title: string;
  url: string | null;
  why: string;
  source: IdeaEvidenceSource;
}

export interface IdeaDetail extends IdeaRead {
  /** markdown：草案为四段式；研究方案为完整结构（[[paper:uuid]] 渲染为库内论文链接） */
  content: string;
  parent_paper_ids: string[];
  parent_papers: IdeaParentPaper[];
  score_rationale: Partial<Record<keyof IdeaScores, string>> | null;
  /** 研究目标（深度生成产物；草案为 null） */
  goal: IdeaGoal | null;
  /** 依据文献（库内 / 外部 / 信号） */
  evidence: IdeaEvidence[] | null;
  /** 深化来源草案（seed.type=idea 时） */
  seed_idea: { id: string; title: string } | null;
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
// Idea 深度生成（Idea 2.0）— docs/api-idea2.md §2
// ============================================================

export type DeepSeedType = 'text' | 'concept' | 'paper' | 'idea';

export interface DeepSeed {
  type: DeepSeedType;
  /** 自由文本，或 concept / paper / idea 的 id */
  value: string;
}

export interface DeepKnobs {
  /** 生成前人工确认研究目标（默认 true） */
  confirm_goal?: boolean;
  /** 目标构建阶段文献工具调用上限 */
  max_tool_calls?: number;
  /** 外部检索（Semantic Scholar / OpenAlex）做相似工作核查 */
  external_search?: boolean;
  /** 评审-修订循环轮数 */
  revise_rounds?: number;
  /** token 预算（超限自动暂停）；null = 不限 */
  budget_tokens?: number | null;
}

export interface DeepIdeaState {
  /** 进行中的深度生成任务（kind=idea_proposal） */
  running_voyage_id: string | null;
  /** 待人工确认的研究目标审批 */
  pending_gate_id: string | null;
  last_run: ForgeLastRun | null;
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
  proxy_url?: string | null;
}

export interface SshCredentialInput {
  name: string;
  host: string;
  port?: number;
  username: string;
  /** PEM 文本，后端 Fernet 加密入库，绝不回传 */
  private_key: string;
  passphrase?: string;
  proxy_url?: string;
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
  /** 连续 N 轮主指标无提升自动停止（M5-A 固定 2） */
  no_improve_stop?: number;
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
  /** 最近一次回写的判定依据（后端可能暂未回传，前端会从 runs[].reflection 兜底） */
  evidence?: string;
}

/** 主指标（docs/api-m5-a.md §1：plan 生成时 LLM 必填；旧实验可能缺失）。 */
export interface PrimaryMetric {
  name: string;
  direction: 'maximize' | 'minimize';
}

/** plan JSON（契约只列字段名，内层结构宽松处理）。 */
export interface ExperimentPlan {
  hypotheses?: ExperimentHypothesis[];
  repro_strategy?: string;
  steps?: (string | { title?: string; desc?: string; description?: string })[];
  budget_estimate?: string | Record<string, unknown>;
  primary_metric?: PrimaryMetric;
}

export type ExperimentRunStatus = 'running' | 'succeeded' | 'failed';

/** 每轮迭代后 AI 的决定：improve 改进 / debug 修错 / stop 停止。 */
export type IterationDecision = 'improve' | 'debug' | 'stop';

export interface ReflectionHypothesisUpdate {
  index: number;
  status: HypothesisStatus;
  evidence?: string;
}

/** 每轮运行后的 LLM 结构化反思（docs/api-m5-a.md §1）。 */
export interface RunReflection {
  observation?: string;
  diagnosis?: string;
  hypothesis_updates?: ReflectionHypothesisUpdate[];
  decision?: IterationDecision;
  planned_change?: string;
  stop_reason?: string | null;
}

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
  /** 该轮的结构化反思（M5-A；进行中/旧数据为 null 或缺失） */
  reflection?: RunReflection | null;
  /** 平台解析出的主指标值（M5-A；解析不到为 null） */
  primary_value?: number | null;
}

export interface ExperimentMetricPoint {
  step: number;
  value: number;
}

/** 实验图表元数据（M5-A figures 步骤产出）；图片本体走 fetchExperimentFigureImage blob。 */
export interface ExperimentFigureInfo {
  index: number;
  name?: string | null;
  caption?: string | null;
}

/** 迭代状态（M5-A）：无提升计数 / 修错计数 / 停止原因。 */
export interface ExperimentIterationState {
  no_improve_streak?: number;
  debug_count?: number;
  stopped_reason?: string | null;
}

export interface ExperimentDetail extends ExperimentRead {
  plan: ExperimentPlan | null;
  runs: ExperimentRunRead[];
  /** markdown 报告，未生成为 null */
  report: string | null;
  /** {指标名: [{step, value}]} */
  metrics: Record<string, ExperimentMetricPoint[]> | null;
  /** 实验图表列表（M5-A；后端未就绪时可能缺失） */
  figures?: ExperimentFigureInfo[];
  /** 迭代状态（M5-A；后端未就绪时可能缺失） */
  iteration_state?: ExperimentIterationState | null;
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
    /** 评测模型：实验代码将获得该模型的 API 访问（工作目录 llm_config.json） */
    eval_model?: string;
    /** HF 镜像：训练类实验经 hf-mirror.com 拉取模型/数据集 */
    hf_mirror?: boolean;
    /** 用户对实验的补充说明（进计划与代码生成 prompt） */
    extra_notes?: string;
  };
}

// ============================================================
// M5-B · Manuscripts（论文撰写）— docs/api-m5-b.md
// ============================================================

/** 会议模板信息（GET /manuscripts/templates）。key 如 neurips2026 / iclr2026 / acl。 */
export interface TemplateInfo {
  key: string;
  name: string;
  page_limit: number | null;
  /** 模板建议的分节顺序（AI 起草可选节来源） */
  sections: string[];
}

export type ManuscriptStatus =
  | 'draft'
  | 'writing'
  | 'compiled'
  | 'under_review'
  | 'approved'
  | 'submitted';

export interface ManuscriptRead {
  id: string;
  project_id: string;
  idea_id: string | null;
  experiment_id: string | null;
  title: string;
  template: string;
  status: ManuscriptStatus;
  /** M5-C：同行评审通过（评分 ≥ 6 且无虚构引用）；后端未升级时缺失 */
  review_passed?: boolean;
  created_at: string;
  updated_at: string;
}

/** 稿件文件元数据（详情内 files[]）。模板样式文件 readonly=true 不可改删。 */
export interface ManuscriptFileMeta {
  id: string;
  path: string;
  size: number;
  updated_at: string;
  readonly?: boolean;
}

/** 单文件内容（编辑器初始加载 / readonly 文件查看用；实时同步走 WS CRDT）。 */
export interface ManuscriptFileRead {
  id: string;
  path: string;
  content: string;
}

export type DiagnosticSeverity = 'error' | 'warning';

export type DiagnosticRule =
  | 'undefined_citation'
  | 'undefined_reference'
  | 'latex_error'
  | 'overfull'
  | 'other';

export interface DiagnosticItem {
  severity: DiagnosticSeverity;
  file: string;
  line: number | null;
  rule: DiagnosticRule;
  message: string;
}

export type CompileStatus = 'ok' | 'error' | 'timeout';

export interface CompileResult {
  version: number;
  status: CompileStatus;
  pdf_available: boolean;
  diagnostics: DiagnosticItem[];
  compiled_at: string;
  duration_ms: number;
}

// —— fact-pack（防幻觉事实源，AI 起草只允许引用其中的引文/图表/数字） ——

export interface FactPackIdea {
  title?: string | null;
  summary?: string | null;
}

export interface FactPackHypothesis {
  text: string;
  status: string;
  evidence?: string | null;
}

export interface FactPackMetricRun {
  seq: number;
  value: number;
}

export interface FactPackMetric {
  name: string;
  runs?: FactPackMetricRun[];
  best?: number | null;
}

export interface FactPackFigure {
  fig_id: string;
  caption?: string | null;
  source?: string | null;
}

export interface FactPackCitation {
  bibkey: string;
  title: string;
  year?: number | null;
}

/** 各分区均可选容错（新建稿件后端异步组装时可能暂缺）。 */
export interface FactPack {
  idea?: FactPackIdea | null;
  hypotheses?: FactPackHypothesis[];
  metrics?: FactPackMetric[];
  figures?: FactPackFigure[];
  citations?: FactPackCitation[];
  generated_at?: string | null;
}

export interface ManuscriptDetail extends ManuscriptRead {
  files: ManuscriptFileMeta[];
  fact_pack: FactPack | null;
  latest_compile: CompileResult | null;
  /** 进行中的 AI 起草任务（kind=paper_writing 的 voyage）；无则 null */
  writing_voyage_id: string | null;
}

export interface CreateManuscriptInput {
  title: string;
  template: string;
  idea_id?: string;
  experiment_id?: string;
}

/** AI 起草入参：sections 为 null/缺省 = 全部节。 */
export interface DraftManuscriptInput {
  sections?: string[] | null;
  notes?: string;
}

// ============================================================
// M5-C · Paper Review（论文同行评审）— docs/api-m5-c.md
// ============================================================

/** 引用存在性：库内/外部精确 | 模糊匹配 | 疑似编造。 */
export type CitationExistence = 'exact' | 'minor' | 'fabricated';

export type CitationSource = 'library' | 's2' | 'openalex' | 'none';

/** 引用支撑性：语境句 + 被引论文摘要 → LLM 判定。 */
export type CitationSupport = 'supported' | 'partial' | 'unsupported' | 'not_checked';

export interface CitationCheckItem {
  bibkey: string;
  existence: CitationExistence;
  matched_title?: string | null;
  source?: CitationSource | null;
  support?: CitationSupport | null;
  /** 引用语境句（cite 前后 2 句），悬停展示 */
  context_snippet?: string | null;
}

export interface CitationCheck {
  total?: number;
  items?: CitationCheckItem[];
}

export type FactCheckKind = 'number_mismatch' | 'unsupported_claim' | 'missing_figure' | 'other';

export type FactCheckSeverity = 'major' | 'minor';

export interface FactCheckItem {
  /** "results.tex:42" 或章节名 */
  location?: string | null;
  issue?: string | null;
  evidence?: string | null;
  kind?: FactCheckKind | string;
  severity?: FactCheckSeverity | string;
}

export interface FactCheck {
  items?: FactCheckItem[];
}

export type ReviewDecisionHint = 'accept' | 'borderline' | 'reject';

/** 汇总评审（meta-review）：三维度 1-4，总评 1-10。 */
export interface MetaReview {
  soundness?: number | null;
  presentation?: number | null;
  contribution?: number | null;
  rating?: number | null;
  decision_hint?: ReviewDecisionHint | null;
  /** markdown 总结 */
  summary?: string | null;
  aggregation?: { ratings?: number[]; method?: string } | null;
}

export interface ReviewGuardrail {
  passed?: boolean;
  regenerated?: number;
}

/** 评审 ReviewSession.payload（各分区可选容错，后端未回传时降级展示）。 */
export interface PaperReviewPayload {
  citation_check?: CitationCheck | null;
  fact_check?: FactCheck | null;
  meta?: MetaReview | null;
  guardrail?: ReviewGuardrail | null;
}

/** 单个评审员的结构化意见（ReviewMessage.content 为其 JSON；解析失败按 markdown 渲染）。 */
export interface ReviewerOpinion {
  soundness?: number;
  presentation?: number;
  contribution?: number;
  rating?: number;
  confidence?: number;
  strengths?: string[];
  weaknesses?: string[];
  questions?: string[];
  /** 可靠性校验未通过：灰显且不计入聚合 */
  unreliable?: boolean;
}

/** GET /manuscripts/{id}/reviews 列表项（历史多轮）；meta / 完整 payload 均容错。 */
export interface ReviewSummary {
  session_id: string;
  created_at: string;
  meta?: MetaReview | null;
  payload?: PaperReviewPayload | null;
  message_count?: number;
}

// ============================================================
// api object
// ============================================================

// ============================================================
// Skills · 技能系统（docs/skill-system.md）
// ============================================================

export type SkillKind = 'guidance' | 'rubric' | 'persona' | 'workflow';
export type SkillScope = 'builtin' | 'user' | 'project';

export interface SkillPersona {
  name: string;
  stance: string;
  style?: string | null;
}

export interface SkillManifest {
  /** 注入点（后端白名单校验） */
  targets: string[];
  config_schema?: Record<string, unknown> | null;
  variables?: string[];
  personas?: SkillPersona[];
  /** workflow 技能的步骤模板（Navigator 步骤 schema） */
  steps?: Record<string, unknown>[];
  output_contract?: Record<string, unknown> | null;
  model_hint?: string | null;
}

export interface SkillRead {
  id: string;
  slug: string;
  kind: SkillKind;
  name: string;
  name_en: string | null;
  description: string | null;
  scope: SkillScope;
  owner_id: string | null;
  project_id: string | null;
  is_archived: boolean;
  created_at: string;
  updated_at: string;
}

export interface SkillVersionRead {
  id: string;
  skill_id: string;
  version: number;
  manifest: SkillManifest;
  body: string;
  changelog: string | null;
  created_by: string | null;
  created_at: string;
}

export interface SkillDetail extends SkillRead {
  current_version: SkillVersionRead | null;
}

export interface ProjectSkillRead {
  id: string;
  project_id: string;
  skill_id: string;
  version_id: string | null;
  target: string;
  config: Record<string, unknown> | null;
  sort_order: number;
  enabled: boolean;
  created_at: string;
  skill: SkillRead | null;
  pinned_version: number | null;
}

export interface SkillTestResult {
  /** 注入到 prompt 的最终文本（persona/workflow 为结构预览） */
  rendered: string;
  output: string | null;
  model: string | null;
}

export const SKILL_KIND_LABEL: Record<SkillKind, string> = {
  guidance: '指引',
  rubric: '评分标准',
  persona: '评审人设',
  workflow: '流程模板',
};

/** 注入点 → 大白话标签（未收录的原样展示）。 */
export const SKILL_TARGET_LABEL: Record<string, string> = {
  'wiki.score_relevance': '文献相关性打分',
  'wiki.compile': '论文笔记编译',
  'forge.gap_analysis': '研究空白分析',
  'forge.generate': '想法生成',
  'forge.score': '想法打分',
  'review.debate': '想法辩论',
  'review.referees': '论文评审员',
  'review.meta_review': '评审汇总',
  'experiment.plan': '实验计划',
  'experiment.setup': '实验搭建',
  'experiment.iterate': '实验迭代',
  'experiment.report': '实验报告',
  'writing.section': '论文分节撰写',
  'writing.related_work': '相关工作综述',
  'navigator.free_plan': '自由任务规划',
};

export function skillTargetLabel(target: string): string {
  return SKILL_TARGET_LABEL[target] ?? target;
}

export interface SkillListingRead {
  id: string;
  skill_id: string;
  skill_version_id: string;
  summary: string | null;
  tags: string[] | null;
  status: 'pending' | 'approved' | 'rejected' | 'delisted';
  install_count: number;
  published_by: string | null;
  comment: string | null;
  created_at: string;
  skill: SkillRead | null;
  version: number | null;
  rating_avg: number | null;
  rating_count: number;
}

export interface SkillListingDetail extends SkillListingRead {
  manifest: SkillManifest | null;
  body: string | null;
}

export interface SkillRatingRead {
  id: string;
  listing_id: string;
  user_id: string;
  rating: number;
  comment: string | null;
  created_at: string;
}

/** 跨部署分享的技能包（format 固定 polaris-skill@1）。 */
export interface SkillExportData {
  format: string;
  slug: string;
  kind: SkillKind;
  name: string;
  name_en?: string | null;
  description?: string | null;
  version?: number | null;
  manifest: SkillManifest;
  body: string;
}

// —— 全局搜索（顶栏 ⌘K）——
export type GlobalSearchHitType = 'paper' | 'concept' | 'idea' | 'experiment' | 'voyage' | 'manuscript';

export interface GlobalSearchHit {
  type: GlobalSearchHitType;
  id: string;
  title: string;
  snippet: string | null;
  status: string | null;
}

export interface GlobalSearchResponse {
  query: string;
  hits: GlobalSearchHit[];
}

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
  updateMe(input: { display_name?: string }): Promise<UserRead> {
    return requestJson<UserRead>('/users/me', 'PATCH', input);
  },
  uploadAvatar(file: File): Promise<UserRead> {
    const form = new FormData();
    form.append('file', file);
    return request<UserRead>('/users/me/avatar', { method: 'POST', body: form });
  },
  avatarBlob(userId: string): Promise<Blob> {
    return requestBlob(`/users/${userId}/avatar`);
  },
  myUsage(): Promise<UsageSummary> {
    return request<UsageSummary>('/users/me/usage');
  },

  // —— 邀请链接 ——
  createInvite(projectId: string, input: { expires_days?: number | null; max_uses?: number | null }): Promise<InviteRead> {
    return requestJson<InviteRead>(`/projects/${projectId}/invites`, 'POST', input);
  },
  listInvites(projectId: string): Promise<InviteRead[]> {
    return request<InviteRead[]>(`/projects/${projectId}/invites`);
  },
  revokeInvite(projectId: string, inviteId: string): Promise<void> {
    return request<void>(`/projects/${projectId}/invites/${inviteId}`, { method: 'DELETE' });
  },
  inviteInfo(token: string): Promise<InviteInfo> {
    return request<InviteInfo>(`/invites/${token}`);
  },
  acceptInvite(token: string): Promise<ProjectRead> {
    return request<ProjectRead>(`/invites/${token}/accept`, { method: 'POST' });
  },

  // —— 管理员：用户管理 ——
  adminListUsers(): Promise<AdminUserRead[]> {
    return request<AdminUserRead[]>('/admin/users');
  },
  adminUpdateUser(
    userId: string,
    input: {
      display_name?: string;
      role?: string;
      is_active?: boolean;
      token_quota?: number;
      features?: Record<string, boolean>;
      llm_access?: string;
    },
  ): Promise<AdminUserRead> {
    return requestJson<AdminUserRead>(`/admin/users/${userId}`, 'PATCH', input);
  },
  adminListProjects(): Promise<ProjectRead[]> {
    return request<ProjectRead[]>('/admin/projects');
  },
  adminBatchAssign(input: { user_ids: string[]; project_ids: string[]; role?: string }): Promise<{ added: number }> {
    return requestJson<{ added: number }>('/admin/users/batch-assign', 'POST', input);
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
  /** 删除研究方向（仅 owner / 平台 admin），方向下的论文、概念、任务等一并删除。 */
  deleteProject(id: string): Promise<void> {
    return request<void>(`/projects/${id}`, { method: 'DELETE' });
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

  // —— 全局搜索（顶栏 ⌘K）：论文/概念/想法/实验/AI 任务/稿件跨实体检索 ——
  globalSearch(projectId: string, q: string, limit = 5): Promise<GlobalSearchResponse> {
    const params = new URLSearchParams({ q, limit: String(limit) });
    return request<GlobalSearchResponse>(`/projects/${projectId}/global-search?${params}`);
  },

  // —— M2 · Papers ——
  listPapers(
    projectId: string,
    opts: {
      status?: PaperStatusFilter;
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
      /** 高级检索：作者 / 机构（包含匹配）、发表时间与入库时间范围（ISO） */
      author?: string;
      affiliation?: string;
      published_from?: string;
      published_to?: string;
      created_from?: string;
      created_to?: string;
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
    if (opts.author) params.set('author', opts.author);
    if (opts.affiliation) params.set('affiliation', opts.affiliation);
    if (opts.published_from) params.set('published_from', opts.published_from);
    if (opts.published_to) params.set('published_to', opts.published_to);
    if (opts.created_from) params.set('created_from', opts.created_from);
    if (opts.created_to) params.set('created_to', opts.created_to);
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

  /** 用最新的图文模式重写 wiki 页（docs/api-lit.md §6.6）：重跑图片筛选注释 + 图文编译，覆盖 wiki_content；同步调用，约 1 分钟。 */
  recompilePaper(id: string): Promise<PaperDetail> {
    return request<PaperDetail>(`/papers/${id}/recompile`, { method: 'POST' });
  },
  /** 删除论文（清理落盘文件，笔记/标签/分段级联删除）。 */
  deletePaper(id: string): Promise<void> {
    return request<void>(`/papers/${id}`, { method: 'DELETE' });
  },
  /** 批量删除：默认软删（移入垃圾桶，可召回）；hard=true 彻底删除。 */
  batchDeletePapers(projectId: string, paperIds: string[], hard = false): Promise<{ deleted: number }> {
    return requestJson<{ deleted: number }>(`/projects/${projectId}/papers/batch-delete`, 'POST', {
      paper_ids: paperIds,
      hard,
    });
  },
  /** 从垃圾桶召回（已编译回 compiled、打过分回 scored、否则按人工精选）。 */
  restorePaper(id: string): Promise<PaperDetail> {
    return request<PaperDetail>(`/papers/${id}/restore`, { method: 'POST' });
  },
  /** 清空垃圾桶：彻底删除项目内全部已删除论文。 */
  emptyTrash(projectId: string): Promise<{ deleted: number }> {
    return request<{ deleted: number }>(`/projects/${projectId}/trash/empty`, { method: 'POST' });
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
    opts: { format: CitationFormat; status?: PaperStatusFilter; tag?: string; starred?: boolean; ids?: string[] },
  ): Promise<Blob> {
    const params = new URLSearchParams({ format: opts.format });
    if (opts.status) params.set('status', opts.status);
    if (opts.tag) params.set('tag', opts.tag);
    if (opts.starred) params.set('starred', 'true');
    if (opts.ids?.length) params.set('ids', opts.ids.join(','));
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

  // —— 知识图谱 ——
  getProjectGraph(projectId: string): Promise<GraphData> {
    return request<GraphData>(`/projects/${projectId}/graph`);
  },

  // —— 文献知识底座：全文索引重建（对话走 sse.ts chatLibrarySse） ——
  rebuildFulltextIndex(projectId: string): Promise<RebuildIndexResult> {
    return request<RebuildIndexResult>(`/projects/${projectId}/index/rebuild`, { method: 'POST' });
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

  // —— Idea 深度生成（Idea 2.0）——
  /** 发起深度生成（kind=idea_proposal）；并发冲突 409；seed 引用对象不存在 404。 */
  startDeepIdea(projectId: string, input: { seed: DeepSeed; knobs?: DeepKnobs }): Promise<VoyageRead> {
    return requestJson<VoyageRead>(`/projects/${projectId}/ideas/deep`, 'POST', input);
  },
  getDeepIdeaState(projectId: string): Promise<DeepIdeaState> {
    return request<DeepIdeaState>(`/projects/${projectId}/ideas/deep/state`);
  },

  // —— M3 · Ideas ——
  listIdeas(
    projectId: string,
    opts: { status?: IdeaStatus; sort?: IdeaSort; depth?: IdeaDepth; research_type?: string } = {},
  ): Promise<IdeaRead[]> {
    const params = new URLSearchParams();
    if (opts.status) params.set('status', opts.status);
    if (opts.sort) params.set('sort', opts.sort);
    if (opts.depth) params.set('depth', opts.depth);
    if (opts.research_type) params.set('research_type', opts.research_type);
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
  /** 单张实验图表 PNG（blob → objectURL 显示，模式同论文 figures）。 */
  fetchExperimentFigureImage(id: string, index: number): Promise<Blob> {
    return requestBlob(`/experiments/${id}/figures/${index}/image`);
  },

  // —— M5-B · Manuscripts（论文撰写） ——
  listManuscriptTemplates(): Promise<TemplateInfo[]> {
    return request<TemplateInfo[]>('/manuscripts/templates');
  },
  createManuscript(projectId: string, input: CreateManuscriptInput): Promise<ManuscriptRead> {
    return requestJson<ManuscriptRead>(`/projects/${projectId}/manuscripts`, 'POST', input);
  },
  listManuscripts(projectId: string): Promise<ManuscriptRead[]> {
    return request<ManuscriptRead[]>(`/projects/${projectId}/manuscripts`);
  },
  getManuscript(id: string): Promise<ManuscriptDetail> {
    return request<ManuscriptDetail>(`/manuscripts/${id}`);
  },
  patchManuscript(id: string, input: { title?: string }): Promise<ManuscriptRead> {
    return requestJson<ManuscriptRead>(`/manuscripts/${id}`, 'PATCH', input);
  },
  /** 仅 owner/admin。 */
  deleteManuscript(id: string): Promise<void> {
    return request<void>(`/manuscripts/${id}`, { method: 'DELETE' });
  },

  // —— M5-B · 稿件文件 ——
  getManuscriptFile(id: string, fid: string): Promise<ManuscriptFileRead> {
    return request<ManuscriptFileRead>(`/manuscripts/${id}/files/${fid}`);
  },
  createManuscriptFile(id: string, input: { path: string; content?: string }): Promise<ManuscriptFileMeta> {
    return requestJson<ManuscriptFileMeta>(`/manuscripts/${id}/files`, 'POST', input);
  },
  /** 重命名（readonly 文件后端会拒绝）。 */
  renameManuscriptFile(id: string, fid: string, path: string): Promise<ManuscriptFileMeta> {
    return requestJson<ManuscriptFileMeta>(`/manuscripts/${id}/files/${fid}`, 'PATCH', { path });
  },
  deleteManuscriptFile(id: string, fid: string): Promise<void> {
    return request<void>(`/manuscripts/${id}/files/${fid}`, { method: 'DELETE' });
  },

  // —— M5-B · fact-pack / 编译 / AI 起草 / 投稿 ——
  /** 重新从 experiment + 文献库组装事实包；前端以 invalidate 详情为准。 */
  refreshFactPack(id: string): Promise<FactPack> {
    return request<FactPack>(`/manuscripts/${id}/fact-pack/refresh`, { method: 'POST' });
  },
  /** 同步编译（tectonic，硬超时 120s），直接返回诊断结果。 */
  compileManuscript(id: string): Promise<CompileResult> {
    return request<CompileResult>(`/manuscripts/${id}/compile`, { method: 'POST' });
  },
  getLatestCompile(id: string): Promise<CompileResult> {
    return request<CompileResult>(`/manuscripts/${id}/compile/latest`);
  },
  /** 最新成功版 PDF（blob → objectURL 喂 iframe）。从未编译成功 → 404。 */
  fetchManuscriptPdf(id: string): Promise<Blob> {
    return requestBlob(`/manuscripts/${id}/pdf`);
  },
  /** AI 起草：创建 kind=paper_writing 的任务；同稿件已有进行中任务 → 409。 */
  draftManuscript(id: string, input: DraftManuscriptInput): Promise<VoyageRead> {
    return requestJson<VoyageRead>(`/manuscripts/${id}/draft`, 'POST', input);
  },
  /** 投稿：创建 paper_submission 审批；未通过同行评审 → 409 REVIEW_REQUIRED（M5-C 前为 COMPILE_REQUIRED）。 */
  submitManuscript(id: string): Promise<GateRead> {
    return request<GateRead>(`/manuscripts/${id}/submit`, { method: 'POST' });
  },

  // —— M5-C · 论文同行评审 ——
  /** 发起同行评审（kind=paper_review 任务）：同稿件已有进行中 → 409；最新编译非 ok → 409 COMPILE_REQUIRED。 */
  startManuscriptReview(id: string, personas?: ReviewPersona[] | null): Promise<VoyageRead> {
    return requestJson<VoyageRead>(`/manuscripts/${id}/review`, 'POST', { personas: personas ?? null });
  },
  /** 历史评审轮次列表；单轮详情复用 GET /sessions/{sid}/messages。 */
  listManuscriptReviews(id: string): Promise<ReviewSummary[]> {
    return request<ReviewSummary[]>(`/manuscripts/${id}/reviews`);
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

  // —— Skills · 技能（docs/skill-system.md §4） ——
  listSkills(opts: { scope?: 'builtin' | 'mine'; kind?: SkillKind; q?: string } = {}): Promise<SkillRead[]> {
    const params = new URLSearchParams();
    if (opts.scope) params.set('scope', opts.scope);
    if (opts.kind) params.set('kind', opts.kind);
    if (opts.q) params.set('q', opts.q);
    const qs = params.toString();
    return request<SkillRead[]>(`/skills${qs ? `?${qs}` : ''}`);
  },
  createSkill(input: {
    slug: string;
    kind: SkillKind;
    name: string;
    name_en?: string;
    description?: string;
    manifest: SkillManifest;
    body: string;
  }): Promise<SkillDetail> {
    return requestJson<SkillDetail>('/skills', 'POST', input);
  },
  getSkill(id: string): Promise<SkillDetail> {
    return request<SkillDetail>(`/skills/${id}`);
  },
  listSkillVersions(id: string): Promise<SkillVersionRead[]> {
    return request<SkillVersionRead[]>(`/skills/${id}/versions`);
  },
  /** 编辑技能 = 追加新版本（版本不可变）。 */
  addSkillVersion(
    id: string,
    input: { manifest: SkillManifest; body: string; changelog?: string },
  ): Promise<SkillVersionRead> {
    return requestJson<SkillVersionRead>(`/skills/${id}/versions`, 'POST', input);
  },
  /** 复制为我的技能（内置技能的编辑路径）。 */
  forkSkill(id: string): Promise<SkillDetail> {
    return request<SkillDetail>(`/skills/${id}/fork`, { method: 'POST' });
  },
  archiveSkill(id: string): Promise<void> {
    return request<void>(`/skills/${id}`, { method: 'DELETE' });
  },
  /** 试运行：预览注入文本；指引/评分类会真实调用一次模型。 */
  testSkill(id: string, input: { target?: string; goal?: string } = {}): Promise<SkillTestResult> {
    return requestJson<SkillTestResult>(`/skills/${id}/test`, 'POST', input);
  },
  /** 运行流程技能：以其步骤为计划创建 AI 任务。 */
  runWorkflowSkill(
    id: string,
    input: { project_id: string; goal: string; vars?: Record<string, string> },
  ): Promise<VoyageRead> {
    return requestJson<VoyageRead>(`/skills/${id}/run`, 'POST', input);
  },
  listProjectSkills(projectId: string): Promise<ProjectSkillRead[]> {
    return request<ProjectSkillRead[]>(`/projects/${projectId}/skills`);
  },
  enableProjectSkill(
    projectId: string,
    input: { skill_id: string; target: string; version_id?: string; config?: Record<string, unknown> },
  ): Promise<ProjectSkillRead> {
    return requestJson<ProjectSkillRead>(`/projects/${projectId}/skills`, 'POST', input);
  },
  patchProjectSkill(
    enableId: string,
    input: { enabled?: boolean; config?: Record<string, unknown>; sort_order?: number; unpin_version?: boolean },
  ): Promise<ProjectSkillRead> {
    return requestJson<ProjectSkillRead>(`/project-skills/${enableId}`, 'PATCH', input);
  },
  removeProjectSkill(enableId: string): Promise<void> {
    return request<void>(`/project-skills/${enableId}`, { method: 'DELETE' });
  },
  // —— 论文分享 PPT（文献追踪板块） ——
  /** 发起 PPT 生成任务：single=单篇分享 / survey=多篇主题梳理。 */
  createPresentation(
    projectId: string,
    input: { paper_ids: string[]; mode: 'single' | 'survey'; notes?: string },
  ): Promise<VoyageRead> {
    return requestJson<VoyageRead>(`/projects/${projectId}/presentations`, 'POST', input);
  },
  /** 下载生成的 PPT（blob；未生成完成时 404 FILE_NOT_READY）。 */
  downloadPresentation(voyageId: string): Promise<Blob> {
    return requestBlob(`/presentations/${voyageId}/file`);
  },

  /** 导出技能包 JSON（跨部署分享）。 */
  exportSkill(id: string): Promise<SkillExportData> {
    return request<SkillExportData>(`/skills/${id}/export`);
  },
  /** 导入技能包为我的技能（slug 冲突自动加后缀）。 */
  importSkill(data: SkillExportData): Promise<SkillDetail> {
    return requestJson<SkillDetail>('/skills/import', 'POST', data);
  },

  // —— Skills · 技能市场（docs/skill-system.md §4.3） ——
  /** 发布我的技能到市场（当前版本），管理员审核后全员可安装。 */
  publishSkill(id: string, input: { summary?: string; tags?: string[] } = {}): Promise<SkillListingRead> {
    return requestJson<SkillListingRead>(`/skills/${id}/publish`, 'POST', input);
  },
  listMarketSkills(
    opts: { q?: string; sort?: '-created_at' | 'installs'; status?: 'approved' | 'pending' } = {},
  ): Promise<SkillListingRead[]> {
    const params = new URLSearchParams();
    if (opts.q) params.set('q', opts.q);
    if (opts.sort) params.set('sort', opts.sort);
    if (opts.status) params.set('status', opts.status);
    const qs = params.toString();
    return request<SkillListingRead[]>(`/market/skills${qs ? `?${qs}` : ''}`);
  },
  getMarketSkill(listingId: string): Promise<SkillListingDetail> {
    return request<SkillListingDetail>(`/market/skills/${listingId}`);
  },
  /** 安装 = 拷贝为我的技能。 */
  installMarketSkill(listingId: string): Promise<SkillDetail> {
    return request<SkillDetail>(`/market/skills/${listingId}/install`, { method: 'POST' });
  },
  decideListing(listingId: string, decision: 'approve' | 'reject', comment?: string): Promise<SkillListingRead> {
    return requestJson<SkillListingRead>(`/market/skills/${listingId}/${decision}`, 'POST', comment ? { comment } : {});
  },
  delistListing(listingId: string): Promise<SkillListingRead> {
    return request<SkillListingRead>(`/market/skills/${listingId}`, { method: 'DELETE' });
  },
  listListingReviews(listingId: string): Promise<SkillRatingRead[]> {
    return request<SkillRatingRead[]>(`/market/skills/${listingId}/reviews`);
  },
  addListingReview(listingId: string, input: { rating: number; comment?: string }): Promise<SkillRatingRead> {
    return requestJson<SkillRatingRead>(`/market/skills/${listingId}/reviews`, 'POST', input);
  },
};
