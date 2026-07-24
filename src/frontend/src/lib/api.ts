import { tr } from './i18n';
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
  if (res.status === 401 && token && !path.startsWith('/auth/')) {
    // 会话过期/失效：清 token 并跳登录（避免在登录/注册接口上误触发）
    setToken(null);
    if (typeof window !== 'undefined' && window.location.pathname !== '/login') {
      window.location.assign('/login');
    }
  }
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
  username?: string | null;
  username_locked?: boolean;
  role?: string;
  llm_access?: 'full' | 'chat_only' | 'blocked';
  /** true = 用户自管 LLM 配置；false = 由管理员统一接管（用全局配置） */
  llm_self_managed?: boolean;
  has_avatar?: boolean;
  token_quota?: number | null;
  features?: Record<string, boolean> | null;
  /** 用户个人设置（后端可能暂未返回，可选） */
  settings?: { chat_fulltext_index?: boolean } | null;
}

export interface UsageSummary {
  tokens_used: number;
  token_quota: number | null;
}

export interface AdminUserRead {
  id: string;
  email: string;
  display_name: string;
  username: string | null;
  role: string;
  is_active: boolean;
  has_avatar: boolean;
  llm_access: string;
  /** true = 用户自管 LLM；false = 管理员接管 */
  llm_self_managed: boolean;
  token_quota: number | null;
  features: Record<string, boolean> | null;
  tokens_used: number;
  created_at: string;
}

export interface RegistrationCodeRead {
  id: string;
  code: string;
  note: string;
  expires_at: string | null;
  max_uses: number | null;
  used_count: number;
  revoked: boolean;
  preset_directions: string[];
  status: string; // active | revoked | expired | exhausted
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
  display_name: string;
  username: string;
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

export interface ProjectMemberRead {
  user_id?: string;
  email?: string;
  display_name?: string | null;
  role?: string;
}

export interface ProjectRead {
  id: string;
  name: string;
  statement: string | null;
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

/** 结构化验收检查项；kind: no_error / exit_code / artifact_exists / schema_valid / metric / min_count / llm_rubric（未知 kind 前端原样展示）。 */
export interface VoyageAcceptanceCheck {
  kind: string;
  /** exit_code / metric / min_count */
  value?: unknown;
  /** artifact_exists */
  key?: string;
  /** schema_valid / min_count */
  field?: string;
  required_keys?: string[];
  /** metric */
  name?: string;
  op?: string;
  /** llm_rubric */
  rubric?: string;
  [extra: string]: unknown;
}

/** 步骤验收标准：这一步"怎样算通过"（text 为大白话补充说明）。 */
export interface VoyageAcceptance {
  text?: string | null;
  checks?: VoyageAcceptanceCheck[] | null;
}

/** 单次尝试的归档（attempt 从 1 起）。 */
export interface VoyageStepAttempt {
  attempt: number;
  observation: unknown;
  verdict: VoyageVerdict | null;
  tokens: unknown;
  started_at: string | null;
  finished_at: string | null;
}

/** 步骤溯源：第几次计划调整创建了它（0 = 初始计划）。 */
export interface VoyageStepProvenance {
  plan_iteration: number;
  [extra: string]: unknown;
}

export interface VoyageStepRead {
  id: string;
  /** 创建序（不可变锚点，计划调整后可能不连续） */
  seq: number;
  /** 清单序 = 执行序（渲染排序用这个，不用 seq） */
  rank: number;
  /** 尝试次数（>1 = 出错后带诊断重试过） */
  attempt: number;
  title: string;
  action: string;
  params: unknown;
  /** 验收标准（可能缺失：老数据 / pipeline 简单步骤） */
  acceptance?: VoyageAcceptance | null;
  /** 非空 = 该步需人工审批（如 compute_budget） */
  requires_gate?: string | null;
  /** 溯源：哪次计划调整创建了它 */
  provenance?: VoyageStepProvenance | null;
  observation: unknown;
  verdict: VoyageVerdict | null;
  status: string;
  /** 后端为 {prompt_tokens, completion_tokens} 字典（历史数据可能是数字） */
  tokens: { prompt_tokens?: number; completion_tokens?: number } | number | null;
  /** 每次尝试的归档（>1 条 = 出错后重试过） */
  attempts?: VoyageStepAttempt[] | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface VoyageRead {
  id: string;
  kind: string;
  /** pipeline（固定流程）| template（模板骨架）| loop（动态调整） */
  mode: string;
  goal: string;
  status: VoyageStatus;
  /** 计划调整次数（重规划/动态追加轮次） */
  plan_iteration: number;
  plan: unknown;
  cursor: number | null;
  budget: Record<string, unknown> | null;
  usage: Record<string, unknown> | null;
  project_id: string;
  created_by: string | null;
  created_at: string;
  updated_at: string;
}

/** 一次计划调整的留痕（source: signal=执行结果规则分支 / navigator=AI 调整 / template=模板分支）。 */
export interface VoyagePlanEvent {
  iteration: number;
  source: 'signal' | 'navigator' | 'template' | (string & {});
  reason: string;
  added: number;
  obsoleted: number;
  /** 触发调整的步骤标题 */
  trigger_step: string | null;
  at: string | null;
}

export interface VoyageDetail extends VoyageRead {
  steps: VoyageStepRead[];
  /** 本次任务快照使用的技能（启动时固定，见 docs/skill-system.md §3.2）。 */
  skills?: { slug: string; name: string; kind: string; version: number; target: string }[];
  /** 计划调整历史（无调整为 [] / 缺失） */
  plan_history?: VoyagePlanEvent[] | null;
}

/** 任务终端历史日志的一条：结构化日志行（log）或大模型完整输出（llm）。 */
export interface VoyageTerminalLogRead {
  id: number; // 自增即时间序，前端据此排序
  event: 'log' | 'llm';
  level?: string | null; // log 上色 level
  stage?: string | null; // llm 环节
  message: string;
  at: string;
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
  'rerank',
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
  /** 可用模型 id 列表（null = 未配置） */
  models: string[] | null;
}

export interface LlmProviderInput {
  name: string;
  kind: LlmProviderKind;
  base_url?: string;
  /** 空字符串 = 不变（PATCH 时） */
  api_key?: string;
  enabled: boolean;
  /** 可用模型 id 列表；整体替换（清空传 []） */
  models?: string[];
}

export interface LlmRoute {
  stage: string;
  provider_id: string;
  model: string;
  temperature?: number | null;
}

export type LlmTestCapability = 'chat' | 'embedding' | 'rerank';

export interface LlmTestModelInput {
  provider_id: string;
  model: string;
  capability: LlmTestCapability;
}

export interface LlmTestResult {
  ok: boolean;
  latency_ms: number;
  error?: string | null;
}

/** 用户 LLM 接管状态：self_managed=true 为自管，false 为管理员接管。 */
export interface LlmManagedStatus {
  self_managed: boolean;
}

/** 当前**生效**的 LLM 配置（key 已掩码，只读展示用）。 */
export interface LlmSelfConfig {
  self_managed: boolean;
  providers: LlmProviderRead[];
  routes: LlmRoute[];
}

/** 测试当前用户在某 stage 上**实际生效**的那条路由的结果。 */
export interface EffectiveTestResult {
  ok: boolean;
  latency_ms: number;
  error: string | null;
  model: string;
  provider_name: string;
  is_fake: boolean;
}

export interface LlmUsageRow {
  date: string;
  stage: string;
  model: string;
  prompt_tokens: number;
  completion_tokens: number;
  calls: number;
}

export interface LlmCallLogSettings {
  enabled: boolean;
}

export interface LlmCallLogRow {
  id: string;
  created_at: string;
  stage: string;
  provider_name: string;
  model: string;
  duration_ms: number;
  status: 'ok' | 'error';
  error: string | null;
  prompt_tokens: number;
  completion_tokens: number;
  user_id: string | null;
  project_id: string | null;
  voyage_id: string | null;
  request_preview: string;
  response_preview: string;
}

export interface LlmCallLogPage {
  total: number;
  items: LlmCallLogRow[];
}

export interface LlmCallLogMessage {
  role: string;
  content: string;
}

/** 详情端点的 request：complete/stream 为 messages（图片只留占位符）；
    embed/rerank 为摘要字段（texts_count/first_text/query…）。 */
export interface LlmCallLogDetail {
  id: string;
  created_at: string;
  stage: string;
  provider_name: string;
  model: string;
  duration_ms: number;
  status: 'ok' | 'error';
  error: string | null;
  prompt_tokens: number;
  completion_tokens: number;
  user_id: string | null;
  project_id: string | null;
  voyage_id: string | null;
  request: { messages?: LlmCallLogMessage[]; images?: string[]; [k: string]: unknown } | null;
  response: string | null;
}

// ============================================================
// M2 · Papers（论文库）— docs/api-m2.md
// ============================================================

export type PaperStatus = 'candidate' | 'scored' | 'excluded' | 'fetched' | 'compiled' | 'included';

/** 状态组别名（docs/api-lit.md §8.5）：visible=检索到的全部（不含垃圾桶）；
    library=库内（达标及之后）；pending_compile=待编译。 */
export type PaperStatusFilter =
  | PaperStatus
  | 'visible'
  | 'library'
  | 'pending_compile'
  | 'compiled_any';

export type PaperSort = 'relevance' | '-published_at';

export interface PaperAuthor {
  name: string;
}

export interface PaperRead {
  id: string;
  /** 本次访问解析出的课题上下文；书架/个人库可达的无库论文（个人补充）为 null */
  project_id: string | null;
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
  /** 垃圾桶原因（status=excluded 时有值）：irrelevant 相关性不足 | manual 手动删除 */
  trash_reason?: 'irrelevant' | 'manual' | null;
  tldr: string | null;
  has_wiki: boolean;
  /** 入库时间 */
  created_at: string;
  /** wiki 编译时间；未编译为 null（旧后端可能缺失） */
  compiled_at?: string | null;
  /** 编译所用模型名；未编译/存量数据为 null（旧后端可能缺失） */
  compiled_model?: string | null;
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
  /** 手动添加后若仍需分阶段后处理，返回可订阅进度的任务 id；已处理完整时为 null。 */
  task_id?: string | null;
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

/* —— PDF 划线标注 —— */
export type HighlightColor = 'yellow' | 'green' | 'blue' | 'pink' | 'purple';
/** 标注样式：高亮块 / 下方横线 / 下方波浪线 */
export type HighlightStyle = 'highlight' | 'underline' | 'wave';

/** 归一化矩形（相对页面左上角，值域 0..1；每行一个）。 */
export interface HighlightRect {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

export interface HighlightRead {
  id: string;
  paper_id: string;
  author_id: string;
  author_name: string;
  page: number; // 1-indexed
  rects: HighlightRect[];
  selected_text: string;
  color: HighlightColor;
  style: HighlightStyle;
  note: string | null;
  created_at: string;
  updated_at: string;
}

export interface HighlightCreateInput {
  page: number;
  rects: HighlightRect[];
  selected_text: string;
  color?: HighlightColor;
  style?: HighlightStyle;
  note?: string | null;
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
  /** 概念所属库回指的课题；共享库（无课题回指）为 null */
  project_id: string | null;
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

/** 全库概念补建结果（POST /projects/{id}/concepts/relink）。 */
export interface ConceptRelinkResult {
  papers: number;
  concepts_created: number;
  links_created: number;
  new_concepts: string[];
}

// ============================================================
// P5c · 共享方向库（/libraries，全实验室可读）
// ============================================================

export interface DirectionLibrarySummary {
  id: string;
  name: string;
  statement: string | null;
  /** 背后课题（过渡期隐式库 1:1 回指；未来共享库可为 null） */
  project_id: string | null;
  /** 是否「我的课题的库」（请求者是背后课题成员 → 显示管理页签） */
  is_mine: boolean;
  /** 是否可管理本库：成员 ∪ 文献库管理员 ∪ 创建者 ∪ 平台管理员（P6/P9b） */
  can_manage: boolean;
  /** 归属模型：个人库 false（归属人=owner_name）| 公共库 true（实验室/全体 admin 所有） */
  is_public: boolean;
  /** 归属人名（个人库=创建者；公共库=原创建者/策展人；可能为空） */
  owner_name: string | null;
  /** 请求者是否本库归属人（submitted_by==我）：个人库删除 / 申请转公共入口据此判定 */
  is_owner: boolean;
  /** 生命周期（P9b）：pending 待审批 | active 已激活 | rejected 已驳回 */
  status: 'pending' | 'active' | 'rejected';
  /** 驳回理由（status=rejected 时有值） */
  review_note: string | null;
  /** 库创建者（用户建库；pending/rejected 库仅创建者 + 平台管理员可见） */
  submitted_by: string | null;
  paper_count: number;
  concept_count: number;
  last_compiled_at: string | null;
  /** 上次同步时间 */
  last_synced_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface DirectionLibraryDetail extends DirectionLibrarySummary {
  cadence: string | null;
  /** 每月 AI 预算（token 数；null = 不限） */
  monthly_budget: number | null;
  /** 收录配置全量（P8：库为权威源），供「收录设置」编辑 */
  definition: ProjectDefinition | null;
}

/** 文献库管理员（后端叫 curator）。 */
export interface LibraryCuratorRead {
  user_id: string;
  email: string;
  display_name: string | null;
}

/** 重复候选组里的一行（对比要素）。 */
export interface DuplicateCandidatePaper {
  id: string;
  title: string;
  year: number | null;
  source: string | null;
  arxiv_id: string | null;
  doi: string | null;
  status: string;
  chunk_count: number;
  has_wiki: boolean;
  created_at: string;
}

export interface DuplicateCandidateGroup {
  /** 按何种键判定为疑似重复：arxiv | doi | title */
  reason: string;
  /** 首行 = 建议保留行（更完整优先） */
  papers: DuplicateCandidatePaper[];
}

export interface PaperMergeResult {
  kept_id: string;
  dropped_id: string;
  dropped_dedup_key: string | null;
  details: Record<string, unknown>;
}

/** 库预算面板：本月 AI 用量（token）与上限。 */
export interface LibraryBudgetRead {
  /** 如 "2026-07" */
  month: string;
  monthly_budget: number | null;
  prompt_tokens: number;
  completion_tokens: number;
  used_tokens: number;
  /** 不限时为 null */
  remaining_tokens: number | null;
  /** true = 本月预算已用尽（同步任务会被拒绝启动） */
  exhausted: boolean;
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
// P5a · 课题「相关研究」书架 — /projects/{pid}/shelf
// ============================================================

/** 书架条目 wiki 来源：live=库版实时 | personal=本人个人编译版 | snapshot=只剩入架快照 | none=没有解读。 */
export type ShelfWikiSource = 'live' | 'personal' | 'snapshot' | 'none';

/** 书架排序：added=添加时间 | year=年份 | relevance=相关度 | title=标题（默认 added）。 */
export type ShelfSort = 'added' | 'year' | 'relevance' | 'title';

export interface ShelfItemRead {
  paper_id: string;
  title: string;
  authors: PaperAuthor[];
  year: number | null;
  venue: string | null;
  arxiv_id: string | null;
  doi: string | null;
  url: string | null;
  tldr: string | null;
  /** 课题语境的「为什么相关」备注 */
  note: string | null;
  wiki_source: ShelfWikiSource;
  /** 解析后的 wiki：库版实时 > 个人版 > 快照；none 时为 null */
  wiki_content: string | null;
  /** 入架落快照的时间；没快照为 null */
  snapshot_at: string | null;
  /** 来源方向库（个人补充为 null） */
  source_library_id: string | null;
  added_at: string;
  /** 个人补充入架后若仍需分阶段后处理，返回可订阅进度的任务 id；已处理完整时为 null。 */
  task_id?: string | null;
}

/** 个人版 wiki 按需编译结果（P5b）。 */
export interface PersonalWikiRead {
  paper_id: string;
  wiki_content: string;
  model: string | null;
}

/** 个人补充入库：arXiv 编号 / DOI / 标题至少给一个。 */
export interface ShelfImportInput {
  arxiv_id?: string;
  doi?: string;
  title?: string;
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
  /** 最大化模式：检索/编译不设篇数上限（忽略 max_papers/compile_top_n），预算不设限 */
  unlimited?: boolean;
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
  /** 软删除时间戳；null=活动，非 null=在垃圾箱 */
  trashed_at?: string | null;
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

/** 服务器系统状态（固定模板探测；连接失败 ok=false 只带 detail）。 */
export interface SshSysinfo {
  ok: boolean;
  detail?: string;
  host?: string;
  cpu?: { cores?: number; load_1m?: number; load_5m?: number; load_15m?: number };
  mem?: { total_mib?: number; used_mib?: number; available_mib?: number };
  disks?: { mount: string; total_mib: number; used_mib: number; avail_mib: number }[];
  gpus?: { index: number; mem_total_mib: number; mem_free_mib: number }[];
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
  /** 软删除时间戳；null=活动，非 null=在垃圾箱 */
  trashed_at?: string | null;
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

/** 实验类型（harness 通用化后 plan 回写；老实验可能缺省 → 前端按「未分类」处理）。 */
export type ExperimentKind = 'eval' | 'training' | 'agent' | 'analysis' | 'other';

/** 运行环境：有此字段 = 在预置 docker 镜像里跑；无 = 裸机运行。 */
export interface ExperimentContainer {
  image?: string;
  /** GPU 选择，如 "device=0,1"；也可能是数量（数字） */
  gpus?: string | number;
  shm_size?: string;
  mounts?: unknown;
}

/** 对照实验的一个条件：baseline 对照组 / treatment 处理组。 */
export interface ExperimentCondition {
  name: string;
  role?: 'baseline' | 'treatment';
  description?: string;
}

/** 评测协议：数据集 / 划分 / 指标 / 样本数（复现类实验用；内层宽松）。 */
export interface EvalProtocol {
  dataset?: string;
  split?: string;
  metric?: string;
  n_examples?: number;
  n_samples?: number;
}

/** 计划里用到的数据集。 */
export interface ExperimentDataset {
  name: string;
  purpose?: string;
  size_hint?: string;
}

/** plan JSON（契约只列字段名，内层结构宽松处理）。 */
export interface ExperimentPlan {
  /** 实验类型（缺省 → 未分类） */
  kind?: ExperimentKind | string;
  /** 运行环境（有镜像 = 容器运行；无 = 本机） */
  container?: ExperimentContainer | null;
  hypotheses?: ExperimentHypothesis[];
  /** 对照实验的对照组/处理组（单一配置实验可省略） */
  conditions?: ExperimentCondition[];
  /** 评测协议（复现类实验用） */
  eval_protocol?: EvalProtocol;
  /** 计划用到的数据集 */
  datasets?: ExperimentDataset[];
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

/** 代码浏览：workdir 内一个文件（相对路径 + 字节大小）。 */
export interface ExperimentCodeEntry {
  path: string;
  size: number;
}

/** 代码清单：source=ssh 为服务器实时读取；checkpoint 为离线快照回退。 */
export interface ExperimentCodeListing {
  source: 'ssh' | 'checkpoint';
  workdir?: string | null;
  files: ExperimentCodeEntry[];
}

export interface ExperimentCodeFile {
  path: string;
  source: string;
  binary: boolean;
  truncated: boolean;
  size: number;
  content: string;
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

/**
 * 论文模板信息（GET /manuscripts/templates）。
 * 后端已 DB 化：id 内置=key（neurips2026 等），库内模板=uuid。
 */
export interface TemplateInfo {
  /** 内置=key（neurips2026 等）；库内模板=uuid。创建稿件时传这个。 */
  id: string;
  name: string;
  description: string | null;
  /** builtin=内置；seeded=官方入库；uploaded=用户自定义上传 */
  source: 'builtin' | 'seeded' | 'uploaded';
  scope: 'global' | 'project';
  project_id: string | null;
  /** 编译引擎：tectonic|pdflatex|xelatex|lualatex */
  engine: string;
  page_limit: number | null;
  /** 模板建议的分节顺序（AI 起草可选节来源） */
  sections: string[];
  unofficial: boolean;
  /** 库内模板 true（可下载 zip），内置 false */
  downloadable: boolean;
  file_count: number;
  /** false=官方模板伪条目尚未下载，不能直接用它建稿，需先触发下载 */
  downloaded: boolean;
  /** 未下载官方模板的 manifest key（触发下载用）；已下载/普通模板为 null */
  download_key: string | null;
}

/** 官方模板按需下载进度（POST download / SSE progress）。 */
export interface TemplateDownloadProgress {
  key: string;
  name: string;
  phase: 'pending' | 'downloading' | 'extracting' | 'done' | 'failed';
  /** 0-100 */
  percent: number;
  detail: string;
  /** done 后的真实模板 id（用它建稿） */
  template_id: string | null;
  error: string | null;
}

export type ManuscriptStatus =
  | 'draft'
  | 'writing'
  | 'compiled'
  | 'under_review'
  | 'approved'
  | 'submitted';

/** LaTeX 编译引擎（Overleaf 式每稿件可选）。 */
export type CompileEngine = 'tectonic' | 'pdflatex' | 'xelatex' | 'lualatex';

export interface ManuscriptRead {
  id: string;
  project_id: string;
  idea_id: string | null;
  experiment_id: string | null;
  title: string;
  template: string;
  status: ManuscriptStatus;
  /** 主文件相对路径（编译入口，通常 main.tex），Overleaf 式可切换 */
  main_tex: string;
  /** 编译引擎：tectonic|pdflatex|xelatex|lualatex */
  engine: string;
  /** M5-C：同行评审通过（评分 ≥ 6 且无虚构引用）；后端未升级时缺失 */
  review_passed?: boolean;
  created_at: string;
  updated_at: string;
  /** 移入垃圾箱的时间；null / 缺失表示未删除（仍在活动列表）。 */
  trashed_at?: string | null;
  /** 置顶时间；非空即置顶（活动列表里置顶项排在最前）。 */
  pinned_at?: string | null;
}

/** 稿件文件元数据（详情内 files[]）。模板样式文件 readonly=true 不可改删。 */
export interface ManuscriptFileMeta {
  id: string;
  path: string;
  size: number;
  updated_at: string;
  readonly?: boolean;
  /** 二进制文件（图片/PDF 等）：不进 CRDT 编辑器，只读预览。 */
  is_binary?: boolean;
  /** 文件夹占位记录（树里显示为可折叠目录）。 */
  is_folder?: boolean;
}

/** 稿件协作者（owner 高亮，role 决定能否加人/删人）。 */
export interface CollaboratorRead {
  user_id: string;
  email: string;
  display_name: string;
  role: string;
  is_owner: boolean;
}

/** 协作者搜索结果（GET /collaborators/search）。 */
export interface UserSearchResult {
  id: string;
  email: string;
  display_name: string;
}

/** 协同编辑分享链接（完整 URL = origin + join_path）。 */
export interface ShareLink {
  token: string;
  join_path: string;
  expires_at: string | null;
  max_uses: number | null;
}

/** 单文件内容（编辑器初始加载 / readonly 文件查看用；实时同步走 WS CRDT）。 */
export interface ManuscriptFileRead {
  id: string;
  path: string;
  content: string;
  readonly?: boolean;
}

/** 文件版本快照来源：AI 写入前 / 编译当刻 / 恢复前备份。 */
export type FileVersionOrigin = 'pre_ai' | 'compile' | 'pre_restore';

export interface FileVersionMeta {
  id: string;
  seq: number;
  origin: FileVersionOrigin;
  label: string | null;
  size: number;
  created_by: string | null;
  created_at: string;
}

export interface FileVersionContent extends FileVersionMeta {
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

const SKILL_KIND: Record<SkillKind, { zh: string; en: string }> = {
  guidance: { zh: '指引', en: 'Guidance' },
  rubric: { zh: '评分标准', en: 'Rubric' },
  persona: { zh: '评审人设', en: 'Reviewer persona' },
  workflow: { zh: '流程模板', en: 'Workflow template' },
};

export function skillKindLabel(kind: SkillKind): string {
  const m = SKILL_KIND[kind];
  return m ? tr(m.zh, m.en) : kind;
}

/** 注入点 → 大白话标签（未收录的原样展示）。 */
const SKILL_TARGET: Record<string, { zh: string; en: string }> = {
  'wiki.score_relevance': { zh: '文献相关性打分', en: 'Paper relevance scoring' },
  'wiki.compile': { zh: '论文笔记编译', en: 'Paper note compilation' },
  'forge.gap_analysis': { zh: '研究空白分析', en: 'Research gap analysis' },
  'forge.generate': { zh: '想法生成', en: 'Idea generation' },
  'forge.score': { zh: '想法打分', en: 'Idea scoring' },
  'review.debate': { zh: '想法辩论', en: 'Idea debate' },
  'review.referees': { zh: '论文评审员', en: 'Paper referees' },
  'review.meta_review': { zh: '评审汇总', en: 'Review synthesis' },
  'experiment.plan': { zh: '实验计划', en: 'Experiment planning' },
  'experiment.setup': { zh: '实验搭建', en: 'Experiment setup' },
  'experiment.iterate': { zh: '实验迭代', en: 'Experiment iteration' },
  'experiment.report': { zh: '实验报告', en: 'Experiment report' },
  'writing.section': { zh: '论文分节撰写', en: 'Paper section writing' },
  'writing.related_work': { zh: '相关工作综述', en: 'Related-work survey' },
  'navigator.free_plan': { zh: '自由任务规划', en: 'Free-form task planning' },
};

/** 全部注入点 key（选择器用）。 */
export const SKILL_TARGETS = Object.keys(SKILL_TARGET);

export function skillTargetLabel(target: string): string {
  const m = SKILL_TARGET[target];
  return m ? tr(m.zh, m.en) : target;
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

export interface McpToolParam {
  name: string;
  required: boolean;
  type: string;
  enum: string[] | null;
  description: string | null;
}
export interface McpToolInfo {
  name: string;
  description: string;
  network: boolean;
  read_only: boolean;
  params: McpToolParam[];
}
export interface McpToolsCatalog {
  server: { name: string; version: string };
  protocol_version: string;
  endpoint: string;
  tools: McpToolInfo[];
}

// ============================================================
// 健康检查（版本号等，反馈上下文用）
// ============================================================

export interface HealthInfo {
  status: string;
  version: string;
}

// ============================================================
// 用户反馈（bug / 功能建议 / 界面体验…）+ 管理端 triage
// ============================================================

export type FeedbackType = 'bug' | 'feature' | 'ui' | 'question' | 'perf' | 'task' | 'other';
export type FeedbackSeverity = 'blocker' | 'high' | 'normal' | 'low';
export type FeedbackStatus =
  | 'new'
  | 'triaged'
  | 'in_progress'
  | 'resolved'
  | 'closed'
  | 'wontfix';

export interface FeedbackImageRead {
  id: string;
  seq: number;
}

/** LLM 生成的 GitHub issue 草稿（可编辑后提交建 issue）。 */
export interface IssueDraft {
  title: string;
  body: string;
  labels: string[];
}

export interface FeedbackAuthor {
  id: string;
  display_name: string;
  username: string | null;
}

export interface FeedbackRead {
  id: string;
  type: FeedbackType;
  severity: FeedbackSeverity;
  title: string;
  body: string;
  route: string | null;
  module: string | null;
  context: Record<string, unknown> | null;
  status: FeedbackStatus;
  admin_note: string | null;
  issue_draft: IssueDraft | null;
  github_issue_number: number | null;
  github_issue_url: string | null;
  created_at: string;
  images: FeedbackImageRead[];
  author: FeedbackAuthor | null;
}

/** 提交反馈入参（上下文由前端自动组装，用户不填）。 */
export interface FeedbackSubmitInput {
  type: FeedbackType;
  severity: FeedbackSeverity;
  title: string;
  body: string;
  route?: string | null;
  context?: Record<string, unknown> | null;
}

/** 管理端 triage 补丁（部分更新）。 */
export interface FeedbackPatch {
  status?: FeedbackStatus;
  severity?: FeedbackSeverity;
  type?: FeedbackType;
  admin_note?: string;
}

// ============================================================
// 我的文献库（跨研究方向的个人收藏 + 浏览记录）— issue #108
// ============================================================

export type LibraryTab = 'saved' | 'history';
export type LibrarySort = 'recent' | 'title' | 'visits' | 'year';

export interface LibraryEntry {
  id: string;
  arxiv_id: string | null;
  doi: string | null;
  title: string;
  authors: PaperAuthor[];
  year: number | null;
  venue: string | null;
  abstract: string | null;
  url: string | null;
  tldr: string | null;
  saved: boolean;
  saved_at: string | null;
  note: string | null;
  visit_count: number;
  last_visited_at: string | null;
  /** 最近一次浏览对应的论文 id；为 null 表示源方向已删除，只能走外链 url。 */
  last_paper_id: string | null;
  created_at: string;
}

export interface LibraryState {
  entry_id: string | null;
  saved: boolean;
}

/** 单条详情 = 列表条目字段 + wiki 快照正文（列表响应不含 wiki）。 */
export type LibraryEntryDetail = LibraryEntry & { wiki_content: string | null };

// ============================================================
// 我发表的（作者信息绑定 + 发表同步）— issue #109
// ============================================================

export interface AuthorProfile {
  name_variants: string[];
  affiliations: string[];
  openalex_author_id: string | null;
  orcid: string | null;
  auto_sync: boolean;
  last_synced_at: string | null;
}

/** PUT upsert 的输入（orcid / auto_sync 可省略走后端默认）。 */
export interface AuthorProfileInput {
  name_variants: string[];
  affiliations: string[];
  openalex_author_id: string | null;
  orcid?: string | null;
  auto_sync?: boolean;
}

export type PublicationStatus = 'pending' | 'confirmed' | 'rejected';

export interface Publication {
  id: string;
  arxiv_id: string | null;
  doi: string | null;
  title: string;
  authors: PaperAuthor[];
  year: number | null;
  venue: string | null;
  url: string | null;
  /** 文献库中匹配到的论文 id；非 null 时可直接跳阅读页。 */
  paper_id: string | null;
  cited_by_count: number;
  source: string;
  status: PublicationStatus;
  confirmed_at: string | null;
  created_at: string;
}

export interface PublicationPage extends PageOf<Publication> {
  counts: { pending: number; confirmed: number; rejected: number };
}

// ============================================================
// 每日新论文池（/daily）— arxiv 每日新提交，保留最近 7 天
// ============================================================

/** 点赞人（头像堆展示用）。 */
export interface DailyLiker {
  id: string;
  display_name: string;
  has_avatar: boolean;
}

/** 完整点赞名单里的一行（点赞时间倒序）。 */
export interface DailyLikerFull extends DailyLiker {
  liked_at: string;
}

/** 点/取消赞后的汇总（幂等返回，供乐观更新对账）。 */
export interface DailyLikeState {
  entry_id: string;
  like_count: number;
  liked_by_me: boolean;
  likers_preview: DailyLiker[];
}

export type DailySort = 'likes' | 'date';

export interface DailyPaperItem {
  entry_id: string;
  paper_id: string;
  /** YYYY-MM-DD */
  feed_date: string;
  primary_category: string;
  categories: string[];
  announce_type: 'new' | 'cross';
  title: string;
  authors: PaperAuthor[];
  abstract: string | null;
  year: number | null;
  arxiv_id: string | null;
  url: string | null;
  published_at: string | null;
  has_wiki: boolean;
  like_count: number;
  liked_by_me: boolean;
  /** 预览最多 5 人；自己赞过时排第一 */
  likers_preview: DailyLiker[];
  /** 仅「我赞过的」列表返回 */
  liked_at?: string | null;
}

export interface DailyPaperDetail extends DailyPaperItem {
  wiki_content: string | null;
}

export type DailyPage = PageOf<DailyPaperItem>;

export interface DailyDay {
  /** YYYY-MM-DD */
  date: string;
  count: number;
}

export interface DailyCollectRequest {
  paper_ids: string[];
  direction_library_ids: string[];
  topic_ids: string[];
  personal: boolean;
}

export interface DailyCollectResult {
  target_type: 'library' | 'topic' | 'personal';
  target_id: string | null;
  added: number;
  skipped_existing: number;
  forbidden: boolean;
}

export interface DailyCollectResponse {
  results: DailyCollectResult[];
}

/** 该论文已在哪些收录目标里（树选框预勾选/禁用）。 */
export interface DailyCollectionsRead {
  direction_library_ids: string[];
  topic_ids: string[];
  in_personal: boolean;
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

  /** 注册表单实时检查用户名是否可用。 */
  usernameAvailable(username: string): Promise<{ available: boolean }> {
    return request<{ available: boolean }>(
      `/auth/username-available?username=${encodeURIComponent(username)}`,
    );
  },

  /** 健康检查：{status, version}（反馈上下文里带版本号用）。 */
  health(): Promise<HealthInfo> {
    return request<HealthInfo>('/health');
  },

  /** Current user. */
  me(): Promise<UserRead> {
    return request<UserRead>('/users/me');
  },
  updateMe(input: { display_name?: string }): Promise<UserRead> {
    return requestJson<UserRead>('/users/me', 'PATCH', input);
  },
  /** 个人设置：文献对话是否为论文建立全文索引。 */
  updateMySettings(input: { chat_fulltext_index: boolean }): Promise<UserRead> {
    return requestJson<UserRead>('/users/me/settings', 'PATCH', input);
  },
  setUsername(username: string): Promise<UserRead> {
    return requestJson<UserRead>('/users/me/username', 'PATCH', { username });
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
  myUsageHistory(input: { days: number }): Promise<LlmUsageRow[]> {
    return request<LlmUsageRow[]>(`/users/me/usage/history?days=${input.days}`);
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
  adminCreateUser(input: {
    email: string;
    /** 明文初始密码，至少 8 位 */
    password: string;
    display_name: string;
    username: string;
    role: 'member' | 'admin';
    llm_access: 'full' | 'chat_only' | 'blocked';
    /** 留空 = 不限 */
    token_quota?: number | null;
  }): Promise<AdminUserRead> {
    return requestJson<AdminUserRead>('/admin/users', 'POST', input);
  },
  adminUpdateUser(
    userId: string,
    input: {
      display_name?: string;
      username?: string;
      /** 重置密码，至少 8 位；留空即不改 */
      password?: string;
      role?: string;
      is_active?: boolean;
      token_quota?: number;
      features?: Record<string, boolean>;
      llm_access?: string;
      /** false = 管理员接管，true = 释放给用户自管 */
      llm_self_managed?: boolean;
    },
  ): Promise<AdminUserRead> {
    return requestJson<AdminUserRead>(`/admin/users/${userId}`, 'PATCH', input);
  },
  adminDeleteUser(userId: string): Promise<void> {
    return request<void>(`/admin/users/${userId}`, { method: 'DELETE' });
  },
  adminBatchDeleteUsers(userIds: string[]): Promise<{ deleted: number }> {
    return requestJson<{ deleted: number }>('/admin/users/batch-delete', 'POST', { user_ids: userIds });
  },
  adminListProjects(): Promise<ProjectRead[]> {
    return request<ProjectRead[]>('/admin/projects');
  },
  adminBatchAssign(input: { user_ids: string[]; project_ids: string[]; role?: string }): Promise<{ added: number }> {
    return requestJson<{ added: number }>('/admin/users/batch-assign', 'POST', input);
  },
  adminListRegistrationCodes(): Promise<RegistrationCodeRead[]> {
    return request<RegistrationCodeRead[]>('/admin/registration-codes');
  },
  adminCreateRegistrationCode(input: {
    note?: string;
    expires_days?: number | null;
    max_uses?: number | null;
    preset_directions?: string[];
  }): Promise<RegistrationCodeRead> {
    return requestJson<RegistrationCodeRead>('/admin/registration-codes', 'POST', input);
  },
  adminRevokeRegistrationCode(codeId: string): Promise<void> {
    return request<void>(`/admin/registration-codes/${codeId}`, { method: 'DELETE' });
  },

  // —— 用户反馈（提交入口 + 我的反馈） ——
  submitFeedback(input: FeedbackSubmitInput): Promise<FeedbackRead> {
    return requestJson<FeedbackRead>('/feedback', 'POST', input);
  },
  uploadFeedbackImage(id: string, file: File): Promise<FeedbackImageRead> {
    const form = new FormData();
    form.append('file', file);
    return request<FeedbackImageRead>(`/feedback/${id}/images`, { method: 'POST', body: form });
  },
  myFeedback(): Promise<FeedbackRead[]> {
    return request<FeedbackRead[]>('/feedback/mine');
  },
  feedbackImageBlob(id: string, seq: number): Promise<Blob> {
    return requestBlob(`/feedback/${id}/images/${seq}`);
  },

  // —— 管理员：反馈 triage + 建 issue ——
  adminListFeedback(): Promise<FeedbackRead[]> {
    return request<FeedbackRead[]>('/admin/feedback');
  },
  adminFeedbackGithubStatus(): Promise<{ enabled: boolean }> {
    return request<{ enabled: boolean }>('/admin/feedback/github-status');
  },
  adminUpdateFeedback(id: string, patch: FeedbackPatch): Promise<FeedbackRead> {
    return requestJson<FeedbackRead>(`/admin/feedback/${id}`, 'PATCH', patch);
  },
  adminGenerateIssueDraft(id: string): Promise<IssueDraft> {
    return request<IssueDraft>(`/admin/feedback/${id}/draft`, { method: 'POST' });
  },
  adminCreateIssue(id: string, draft: IssueDraft): Promise<{ number: number; url: string }> {
    return requestJson<{ number: number; url: string }>(`/admin/feedback/${id}/issue`, 'POST', draft);
  },

  // —— Projects ——
  listProjects(): Promise<ProjectRead[]> {
    return request<ProjectRead[]>('/projects');
  },
  createProject(input: {
    name: string;
    statement?: string;
    source_library_ids?: string[];
  }): Promise<ProjectRead> {
    return requestJson<ProjectRead>('/projects', 'POST', input);
  },
  getProject(id: string): Promise<ProjectRead> {
    return request<ProjectRead>(`/projects/${id}`);
  },
  patchProject(
    id: string,
    input: { name?: string; statement?: string; status?: string },
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
  getVoyage(id: string, opts?: { includeObsolete?: boolean }): Promise<VoyageDetail> {
    return request<VoyageDetail>(`/voyages/${id}${opts?.includeObsolete ? '?include_obsolete=true' : ''}`);
  },
  cancelVoyage(id: string): Promise<VoyageRead> {
    return request<VoyageRead>(`/voyages/${id}/cancel`, { method: 'POST' });
  },
  /** 重试 paused_error 的航程，从断点续跑。 */
  resumeVoyage(id: string): Promise<VoyageRead> {
    return request<VoyageRead>(`/voyages/${id}/resume`, { method: 'POST' });
  },
  /** 任务终端历史日志（结构化日志 + 大模型完整输出），供刷新后 / 事后回看。 */
  getVoyageLogs(id: string): Promise<VoyageTerminalLogRead[]> {
    return request<VoyageTerminalLogRead[]>(`/voyages/${id}/logs`);
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
  // —— Lit · PDF 划线标注 ——
  /** 某论文的全部划线（按页码排序）。 */
  listPaperHighlights(paperId: string): Promise<HighlightRead[]> {
    return request<HighlightRead[]>(`/papers/${paperId}/highlights`);
  },
  createPaperHighlight(paperId: string, input: HighlightCreateInput): Promise<HighlightRead> {
    return requestJson<HighlightRead>(`/papers/${paperId}/highlights`, 'POST', input);
  },
  /** 改颜色 / 样式 / 批注（字段缺省表示不改；note 传空串清空批注）。 */
  patchHighlight(
    highlightId: string,
    input: { color?: HighlightColor; style?: HighlightStyle; note?: string | null },
  ): Promise<HighlightRead> {
    return requestJson<HighlightRead>(`/highlights/${highlightId}`, 'PATCH', input);
  },
  deleteHighlight(highlightId: string): Promise<void> {
    return request<void>(`/highlights/${highlightId}`, { method: 'DELETE' });
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
  /** 全库概念补建：对已编译论文重抽 [[双链]]、建缺失概念并补齐关联（幂等）。 */
  relinkConcepts(projectId: string): Promise<ConceptRelinkResult> {
    return request<ConceptRelinkResult>(`/projects/${projectId}/concepts/relink`, {
      method: 'POST',
    });
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

  // —— P5c · 共享方向库（全实验室可读） ——
  /** 文献库列表；可按归属类型（个人/公共）与生命周期状态过滤（仅有值才拼进 query）。 */
  listLibraries(filters?: {
    type?: 'personal' | 'public' | 'all';
    status?: DirectionLibrarySummary['status'];
  }): Promise<DirectionLibrarySummary[]> {
    const params = new URLSearchParams();
    if (filters?.type && filters.type !== 'all') params.set('type', filters.type);
    if (filters?.status) params.set('status', filters.status);
    const qs = params.toString();
    return request<DirectionLibrarySummary[]>(`/libraries${qs ? `?${qs}` : ''}`);
  },
  /** 申请把个人库转为公共库（创建者/策展人→pending；平台 admin 调则直接通过转公共）。 */
  requestPublicLibrary(id: string): Promise<DirectionLibrarySummary> {
    return requestJson<DirectionLibrarySummary>(`/libraries/${id}/request-public`, 'POST', {});
  },
  /** 撤回转公共申请（创建者/策展人）：pending → 退回可用个人库。 */
  cancelRequestPublicLibrary(id: string): Promise<DirectionLibraryDetail> {
    return requestJson<DirectionLibraryDetail>(`/libraries/${id}/cancel-request-public`, 'POST', {});
  },
  /** 把公共库转回个人库（平台 admin）：其他成员将看不到。 */
  makeLibraryPersonal(id: string): Promise<DirectionLibraryDetail> {
    return requestJson<DirectionLibraryDetail>(`/libraries/${id}/make-personal`, 'POST', {});
  },
  getLibrary(id: string): Promise<DirectionLibraryDetail> {
    return request<DirectionLibraryDetail>(`/libraries/${id}`);
  },
  /** 新建文献库（P9b：任意登录用户可建；新库 status=pending 待管理员审批激活）。 */
  createLibrary(input: {
    name: string;
    statement?: string | null;
    rubric?: RubricDimension[];
    anchors?: AnchorPaper[];
    cadence?: string | null;
    monthly_budget?: number | null;
    keywords?: KeywordSpec | null;
  }): Promise<DirectionLibraryDetail> {
    return requestJson<DirectionLibraryDetail>('/libraries', 'POST', input);
  },
  /**
   * AI 根据库名与一句话说明推荐收录设置（分类 / 关键词 / 打分标准 / 锚点论文）。
   * 供建库与收录设置的「AI 自动生成」按钮调用；结果填进表单供用户修改后保存。
   */
  suggestLibraryDefinition(input: { name: string; statement: string }): Promise<{
    keywords: { arxiv_categories: string[]; include: string[] };
    rubric: RubricDimension[];
    anchors: AnchorPaper[];
  }> {
    return requestJson('/libraries/suggest-definition', 'POST', input);
  },
  /** 审批通过（仅平台管理员）：pending/rejected → active，激活后可触发抓取。 */
  approveLibrary(id: string): Promise<DirectionLibraryDetail> {
    return request<DirectionLibraryDetail>(`/libraries/${id}/approve`, { method: 'POST' });
  },
  /** 驳回（仅平台管理员）：→ rejected，可带理由。 */
  rejectLibrary(id: string, note?: string | null): Promise<DirectionLibraryDetail> {
    return requestJson<DirectionLibraryDetail>(`/libraries/${id}/reject`, 'POST', { note: note ?? null });
  },
  /** 对某个方向库直接触发抓取（P9a；仅 status=active 且预算内，可管理者）。 */
  startLibraryIngest(id: string, input: { mode: IngestMode; knobs?: IngestKnobs }): Promise<VoyageRead> {
    return requestJson<VoyageRead>(`/libraries/${id}/ingest/run`, 'POST', input);
  },
  /** 课题当前关联的文献库摘要（顺序 = 关联建立时间）。 */
  getSourceLibraries(projectId: string): Promise<DirectionLibrarySummary[]> {
    return request<DirectionLibrarySummary[]>(`/projects/${projectId}/source-libraries`);
  },
  /** 全量替换课题关联的文献库（空数组合法 = 课题 0 关联）。 */
  setSourceLibraries(projectId: string, libraryIds: string[]): Promise<DirectionLibrarySummary[]> {
    return requestJson<DirectionLibrarySummary[]>(
      `/projects/${projectId}/source-libraries`,
      'PUT',
      { library_ids: libraryIds },
    );
  },
  /** 编辑库信息（可管理者）；传 null 清空对应字段。 */
  updateLibrary(
    id: string,
    input: {
      name?: string;
      statement?: string | null;
      cadence?: string | null;
      monthly_budget?: number | null;
      rubric?: RubricDimension[] | null;
      anchors?: AnchorPaper[] | null;
      keywords?: KeywordSpec | null;
      questions?: string[] | null;
    },
  ): Promise<DirectionLibraryDetail> {
    return requestJson<DirectionLibraryDetail>(`/libraries/${id}`, 'PATCH', input);
  },
  /**
   * 删除文献库（仅平台 admin）。库仍有课题关联且未 force 时后端返回
   * 409 LIBRARY_HAS_TOPICS；传 force=true 连同关联一起删除。返回 204 无 body。
   */
  deleteLibrary(id: string, force = false): Promise<void> {
    return request<void>(`/libraries/${id}${force ? '?force=true' : ''}`, { method: 'DELETE' });
  },
  /** 本月预算消耗（可管理者可见）。 */
  getLibraryBudget(id: string): Promise<LibraryBudgetRead> {
    return request<LibraryBudgetRead>(`/libraries/${id}/budget`);
  },
  /** 库内疑似重复论文（可管理者）。 */
  listDuplicateCandidates(id: string): Promise<DuplicateCandidateGroup[]> {
    return request<DuplicateCandidateGroup[]>(`/libraries/${id}/duplicate-candidates`);
  },
  /** 合并重复论文（不可撤销）：drop 的全部归属并入 keep 后删除 drop。 */
  mergePapers(input: { keep_id: string; drop_id: string }): Promise<PaperMergeResult> {
    return requestJson<PaperMergeResult>('/papers/merge', 'POST', input);
  },
  /** 文献库管理员名单（可管理者可见）。 */
  listLibraryCurators(id: string): Promise<LibraryCuratorRead[]> {
    return request<LibraryCuratorRead[]>(`/libraries/${id}/curators`);
  },
  /** 全量替换文献库管理员名单（仅平台管理员）。 */
  setLibraryCurators(id: string, userIds: string[]): Promise<LibraryCuratorRead[]> {
    return requestJson<LibraryCuratorRead[]>(`/libraries/${id}/curators`, 'PUT', { user_ids: userIds });
  },
  /** 库内论文（缺省只列相关性达标的）。 */
  listLibraryPapers(
    id: string,
    opts: { status?: PaperStatusFilter; q?: string; sort?: PaperSort; page?: number; size?: number } = {},
  ): Promise<PageOf<PaperRead>> {
    const params = new URLSearchParams();
    if (opts.status) params.set('status', opts.status);
    if (opts.q) params.set('q', opts.q);
    if (opts.sort) params.set('sort', opts.sort);
    if (opts.page) params.set('page', String(opts.page));
    if (opts.size) params.set('size', String(opts.size));
    const qs = params.toString();
    return request<PageOf<PaperRead>>(`/libraries/${id}/papers${qs ? `?${qs}` : ''}`);
  },
  listLibraryConcepts(
    id: string,
    opts: { category?: ConceptCategory; q?: string } = {},
  ): Promise<ConceptRead[]> {
    const params = new URLSearchParams();
    if (opts.category) params.set('category', opts.category);
    if (opts.q) params.set('q', opts.q);
    const qs = params.toString();
    return request<ConceptRead[]>(`/libraries/${id}/concepts${qs ? `?${qs}` : ''}`);
  },
  searchLibrary(
    id: string,
    opts: { q: string; mode?: SearchMode; limit?: number },
  ): Promise<SearchResult> {
    const params = new URLSearchParams({ q: opts.q });
    if (opts.mode) params.set('mode', opts.mode);
    if (opts.limit) params.set('limit', String(opts.limit));
    return request<SearchResult>(`/libraries/${id}/search?${params.toString()}`);
  },

  // —— P9d · 独立库文献管理台（镜像 project 作用域的集合端点） ——
  /** 库内论文（全过滤维度，同 listPapers）；status=excluded 为垃圾桶。 */
  listLibraryPapersFull(
    id: string,
    opts: {
      status?: PaperStatusFilter;
      q?: string;
      sort?: PaperSort;
      page?: number;
      size?: number;
      tag?: string;
      starred?: boolean;
      reading_status?: ReadingStatus;
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
    return request<PageOf<PaperRead>>(`/libraries/${id}/papers${qs ? `?${qs}` : ''}`);
  },
  /** 手动添加文献到库；409 → PAPER_EXISTS（body 含 paper_id）；422 → PARSE_FAILED。 */
  importLibraryPaper(id: string, input: PaperImportInput): Promise<PaperDetail> {
    return requestJson<PaperDetail>(`/libraries/${id}/papers`, 'POST', input);
  },
  /** 批量删除库内论文：默认软删（垃圾桶），hard=true 彻底删除。 */
  batchDeleteLibraryPapers(id: string, paperIds: string[], hard = false): Promise<{ deleted: number }> {
    return requestJson<{ deleted: number }>(`/libraries/${id}/papers/batch-delete`, 'POST', {
      paper_ids: paperIds,
      hard,
    });
  },
  /** 清空库垃圾桶：彻底删除库内全部已删除论文。 */
  emptyLibraryTrash(id: string): Promise<{ deleted: number }> {
    return request<{ deleted: number }>(`/libraries/${id}/trash/empty`, { method: 'POST' });
  },
  /** 库标签（独立库不支持标签，恒返回 []）。 */
  listLibraryTags(id: string): Promise<TagRead[]> {
    return request<TagRead[]>(`/libraries/${id}/tags`);
  },
  getLibraryIngestState(id: string): Promise<IngestState> {
    return request<IngestState>(`/libraries/${id}/ingest/state`);
  },
  getLibraryGraph(id: string): Promise<GraphData> {
    return request<GraphData>(`/libraries/${id}/graph`);
  },
  /** 库笔记本：全库笔记分页 + 搜索。 */
  listLibraryNotes(
    id: string,
    opts: { q?: string; paper_id?: string; page?: number; size?: number } = {},
  ): Promise<PageOf<NoteWithPaper>> {
    const params = new URLSearchParams();
    if (opts.q) params.set('q', opts.q);
    if (opts.paper_id) params.set('paper_id', opts.paper_id);
    if (opts.page) params.set('page', String(opts.page));
    if (opts.size) params.set('size', String(opts.size));
    const qs = params.toString();
    return request<PageOf<NoteWithPaper>>(`/libraries/${id}/notes${qs ? `?${qs}` : ''}`);
  },

  // —— P5a · 课题「相关研究」书架 ——
  listShelf(
    projectId: string,
    opts: {
      page?: number;
      size?: number;
      q?: string;
      author?: string;
      affiliation?: string;
      year_from?: number;
      year_to?: number;
      reading_status?: ReadingStatus;
      starred?: boolean;
      sort?: ShelfSort;
    } = {},
  ): Promise<PageOf<ShelfItemRead>> {
    const params = new URLSearchParams();
    if (opts.page) params.set('page', String(opts.page));
    if (opts.size) params.set('size', String(opts.size));
    if (opts.q) params.set('q', opts.q);
    if (opts.author) params.set('author', opts.author);
    if (opts.affiliation) params.set('affiliation', opts.affiliation);
    if (opts.year_from != null) params.set('year_from', String(opts.year_from));
    if (opts.year_to != null) params.set('year_to', String(opts.year_to));
    if (opts.reading_status) params.set('reading_status', opts.reading_status);
    if (opts.starred) params.set('starred', 'true');
    if (opts.sort) params.set('sort', opts.sort);
    const qs = params.toString();
    return request<PageOf<ShelfItemRead>>(`/projects/${projectId}/shelf${qs ? `?${qs}` : ''}`);
  },
  /** 书架全部 paper_id（「已入架」勾选态用）。 */
  listShelfIds(projectId: string): Promise<{ paper_ids: string[] }> {
    return request<{ paper_ids: string[] }>(`/projects/${projectId}/shelf/ids`);
  },
  /** 入架（重复入架幂等更新备注）；后端同步收藏进个人库。 */
  addToShelf(projectId: string, input: { paper_id: string; note?: string }): Promise<ShelfItemRead> {
    return requestJson<ShelfItemRead>(`/projects/${projectId}/shelf`, 'POST', input);
  },
  /** 个人补充入库：查池命中直接入架，未命中抓取解析；422 → PARSE_FAILED。 */
  importToShelf(projectId: string, input: ShelfImportInput): Promise<ShelfItemRead> {
    return requestJson<ShelfItemRead>(`/projects/${projectId}/shelf/import`, 'POST', input);
  },
  updateShelfNote(projectId: string, paperId: string, note: string | null): Promise<ShelfItemRead> {
    return requestJson<ShelfItemRead>(`/projects/${projectId}/shelf/${paperId}`, 'PATCH', { note });
  },
  /** 移出书架：只删书架行，个人库收藏不动。 */
  removeFromShelf(projectId: string, paperId: string): Promise<void> {
    return request<void>(`/projects/${projectId}/shelf/${paperId}`, { method: 'DELETE' });
  },
  /** 手动刷新书架快照：从当前最优 wiki（库版 > 个人版）重拷；无来源 409。 */
  refreshShelfSnapshot(projectId: string, paperId: string): Promise<ShelfItemRead> {
    return request<ShelfItemRead>(`/projects/${projectId}/shelf/${paperId}/refresh-snapshot`, {
      method: 'POST',
    });
  },
  /** 个人版 wiki 按需编译（无库版解读的论文；费用记个人额度）。 */
  compilePersonalWiki(paperId: string, topicId?: string | null): Promise<PersonalWikiRead> {
    return requestJson<PersonalWikiRead>(`/papers/${paperId}/personal-wiki`, 'POST', {
      topic_id: topicId ?? null,
    });
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
  /** 可选全文索引：为本课题「相关研究」这批论文异步建全文索引（设置关时后端 409 INDEXING_DISABLED）。 */
  buildShelfIndex(projectId: string): Promise<{ queued: number }> {
    return requestJson<{ queued: number }>(`/projects/${projectId}/shelf/index/rebuild`, 'POST', {});
  },
  /** 可选全文索引：为「我的收藏」这批个人文献异步建全文索引。 */
  buildPersonalIndex(): Promise<{ queued: number }> {
    return requestJson<{ queued: number }>('/library/index/rebuild', 'POST', {});
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
    opts: {
      status?: IdeaStatus;
      sort?: IdeaSort;
      depth?: IdeaDepth;
      research_type?: string;
      trashed?: boolean;
    } = {},
  ): Promise<IdeaRead[]> {
    const params = new URLSearchParams();
    if (opts.status) params.set('status', opts.status);
    if (opts.sort) params.set('sort', opts.sort);
    if (opts.depth) params.set('depth', opts.depth);
    if (opts.research_type) params.set('research_type', opts.research_type);
    if (opts.trashed) params.set('trashed', 'true');
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
  /** 软删除：移入垃圾箱（仅 owner/admin，否则 403）。 */
  trashIdea(id: string): Promise<void> {
    return request<void>(`/ideas/${id}`, { method: 'DELETE' });
  },
  /** 永久删除，不可恢复（仅 owner/admin，否则 403）。 */
  deleteIdeaPermanent(id: string): Promise<void> {
    return request<void>(`/ideas/${id}?permanent=true`, { method: 'DELETE' });
  },
  /** 从垃圾箱恢复（仅 owner/admin，否则 403）。 */
  restoreIdea(id: string): Promise<IdeaRead> {
    return request<IdeaRead>(`/ideas/${id}/restore`, { method: 'POST' });
  },
  /** 批量操作想法：trash=移入垃圾箱 / restore=恢复 / delete=永久删除（仅 owner/admin，否则 403）。 */
  batchIdeas(
    projectId: string,
    action: 'trash' | 'restore' | 'delete',
    ids: string[],
  ): Promise<{ affected: number }> {
    return requestJson<{ affected: number }>(
      `/projects/${projectId}/ideas/batch`,
      'POST',
      { action, ids },
    );
  },
  /** 清空垃圾箱：永久删除该研究方向下所有已软删除想法（仅 owner/admin，否则 403）。 */
  emptyIdeaTrash(projectId: string): Promise<{ affected: number }> {
    return request<{ affected: number }>(`/projects/${projectId}/ideas/trash/empty`, {
      method: 'POST',
    });
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
  /** 服务器系统状态一览（CPU/内存/磁盘/GPU；连接失败 ok=false）。 */
  getSshCredentialSysinfo(id: string): Promise<SshSysinfo> {
    return request<SshSysinfo>(`/ssh-credentials/${id}/sysinfo`);
  },

  // —— M4 · Experiments ——
  createExperiment(projectId: string, input: CreateExperimentInput): Promise<ExperimentRead> {
    return requestJson<ExperimentRead>(`/projects/${projectId}/experiments`, 'POST', input);
  },
  /** 默认返回活动列表；opts.trashed=true 返回垃圾箱（已软删除的实验）。 */
  listExperiments(projectId: string, opts?: { trashed?: boolean }): Promise<ExperimentRead[]> {
    const qs = opts?.trashed ? '?trashed=true' : '';
    return request<ExperimentRead[]>(`/projects/${projectId}/experiments${qs}`);
  },
  getExperiment(id: string): Promise<ExperimentDetail> {
    return request<ExperimentDetail>(`/experiments/${id}`);
  },
  /** 软删除：移入垃圾箱（仅 owner/admin，否则 403）。 */
  trashExperiment(id: string): Promise<void> {
    return request<void>(`/experiments/${id}`, { method: 'DELETE' });
  },
  /** 永久删除，不可恢复（仅 owner/admin，否则 403）。 */
  deleteExperimentPermanent(id: string): Promise<void> {
    return request<void>(`/experiments/${id}?permanent=true`, { method: 'DELETE' });
  },
  /** 从垃圾箱恢复（仅 owner/admin，否则 403）。 */
  restoreExperiment(id: string): Promise<ExperimentRead> {
    return request<ExperimentRead>(`/experiments/${id}/restore`, { method: 'POST' });
  },
  /** 批量操作实验：trash=移入垃圾箱 / restore=恢复 / delete=永久删除（仅 owner/admin，否则 403）。 */
  batchExperiments(
    projectId: string,
    action: 'trash' | 'restore' | 'delete',
    ids: string[],
  ): Promise<{ affected: number }> {
    return requestJson<{ affected: number }>(
      `/projects/${projectId}/experiments/batch`,
      'POST',
      { action, ids },
    );
  },
  /** 清空垃圾箱：永久删除该研究方向下所有已软删除实验（仅 owner/admin，否则 403）。 */
  emptyExperimentTrash(projectId: string): Promise<{ affected: number }> {
    return request<{ affected: number }>(`/projects/${projectId}/experiments/trash/empty`, {
      method: 'POST',
    });
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
  /** 实验代码文件清单（优先 SSH 实时读 workdir；服务器不可达回退快照）。 */
  getExperimentCode(id: string): Promise<ExperimentCodeListing> {
    return request<ExperimentCodeListing>(`/experiments/${id}/code`);
  },
  /** 实验所在服务器的系统状态（实验搭建/运行期间实时查看）。 */
  getExperimentSysinfo(id: string): Promise<SshSysinfo> {
    return request<SshSysinfo>(`/experiments/${id}/sysinfo`);
  },
  /** 实验代码打包下载（zip）。 */
  fetchExperimentCodeArchive(id: string): Promise<Blob> {
    return requestBlob(`/experiments/${id}/code/archive`);
  },
  /** 单个代码文件原样下载（blob，含二进制）。 */
  fetchExperimentCodeFileRaw(id: string, path: string): Promise<Blob> {
    return requestBlob(`/experiments/${id}/code/file/download?path=${encodeURIComponent(path)}`);
  },
  /** 读实验代码单文件内容（workdir 内相对路径）。 */
  getExperimentCodeFile(id: string, path: string): Promise<ExperimentCodeFile> {
    return request<ExperimentCodeFile>(
      `/experiments/${id}/code/file?path=${encodeURIComponent(path)}`,
    );
  },

  // —— M5-B · Manuscripts（论文撰写） ——
  /** 不带 projectId 只返回内置+全平台模板；带上则并入该研究方向私有上传模板。 */
  listManuscriptTemplates(projectId?: string): Promise<TemplateInfo[]> {
    const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
    return request<TemplateInfo[]>(`/manuscripts/templates${qs}`);
  },
  /** 上传模板 zip。给 project_id=项目私有；不给=全平台（需平台管理员，否则 403 ADMIN_REQUIRED_FOR_GLOBAL）。zip 无 .tex → 422。 */
  uploadManuscriptTemplate(input: {
    file: File;
    name: string;
    description?: string;
    engine?: string;
    page_limit?: number;
    project_id?: string;
  }): Promise<TemplateInfo> {
    const form = new FormData();
    form.append('file', input.file);
    form.append('name', input.name);
    if (input.description) form.append('description', input.description);
    if (input.engine) form.append('engine', input.engine);
    if (input.page_limit != null) form.append('page_limit', String(input.page_limit));
    if (input.project_id) form.append('project_id', input.project_id);
    return request<TemplateInfo>('/manuscripts/templates', { method: 'POST', body: form });
  },
  /** 下载库内模板 zip（downloadable=true 的模板）。 */
  downloadManuscriptTemplate(id: string): Promise<Blob> {
    return requestBlob(`/manuscripts/templates/${id}/download`);
  },
  /** 触发官方模板按需下载（幂等）；已下载则直接返回 phase="done" 带 template_id；未知 key → 404。 */
  startTemplateDownload(key: string): Promise<TemplateDownloadProgress> {
    return request<TemplateDownloadProgress>(`/manuscripts/templates/download/${encodeURIComponent(key)}`, {
      method: 'POST',
    });
  },
  /** 删除模板（创建者/项目管理者/平台 admin 可删）。 */
  deleteManuscriptTemplate(id: string): Promise<void> {
    return request<void>(`/manuscripts/templates/${id}`, { method: 'DELETE' });
  },
  createManuscript(projectId: string, input: CreateManuscriptInput): Promise<ManuscriptRead> {
    return requestJson<ManuscriptRead>(`/projects/${projectId}/manuscripts`, 'POST', input);
  },
  /** 默认返回活动列表；opts.trashed=true 返回垃圾箱（已软删除的稿件）。 */
  listManuscripts(projectId: string, opts?: { trashed?: boolean }): Promise<ManuscriptRead[]> {
    const qs = opts?.trashed ? '?trashed=true' : '';
    return request<ManuscriptRead[]>(`/projects/${projectId}/manuscripts${qs}`);
  },
  getManuscript(id: string): Promise<ManuscriptDetail> {
    return request<ManuscriptDetail>(`/manuscripts/${id}`);
  },
  patchManuscript(
    id: string,
    input: { title?: string; main_tex?: string; engine?: CompileEngine; pinned?: boolean },
  ): Promise<ManuscriptRead> {
    return requestJson<ManuscriptRead>(`/manuscripts/${id}`, 'PATCH', input);
  },
  /** 仅 owner/admin。 */
  deleteManuscript(id: string): Promise<void> {
    return request<void>(`/manuscripts/${id}`, { method: 'DELETE' });
  },
  /** 软删除：移入垃圾箱（仅 owner/admin，否则 403）。 */
  trashManuscript(id: string): Promise<void> {
    return request<void>(`/manuscripts/${id}`, { method: 'DELETE' });
  },
  /** 永久删除，不可恢复（仅 owner/admin，否则 403）。 */
  deleteManuscriptPermanent(id: string): Promise<void> {
    return request<void>(`/manuscripts/${id}?permanent=true`, { method: 'DELETE' });
  },
  /** 从垃圾箱恢复（仅 owner/admin，否则 403）。 */
  restoreManuscript(id: string): Promise<ManuscriptRead> {
    return request<ManuscriptRead>(`/manuscripts/${id}/restore`, { method: 'POST' });
  },
  /** 批量操作稿件：trash=移入垃圾箱 / restore=恢复 / delete=永久删除（仅 owner/admin，否则 403）。 */
  batchManuscripts(
    projectId: string,
    action: 'trash' | 'restore' | 'delete',
    ids: string[],
  ): Promise<{ affected: number }> {
    return requestJson<{ affected: number }>(
      `/projects/${projectId}/manuscripts/batch`,
      'POST',
      { action, ids },
    );
  },
  /** 清空垃圾箱：永久删除该研究方向下所有已软删除稿件（仅 owner/admin，否则 403）。 */
  emptyManuscriptTrash(projectId: string): Promise<{ affected: number }> {
    return request<{ affected: number }>(`/projects/${projectId}/manuscripts/trash/empty`, {
      method: 'POST',
    });
  },
  /**
   * 把主文件 \begin{document}…\end{document} 之间的正文重写为
   * POLARIS_SECTION 标记的结构化骨架（供分节 AI 起草）。
   * 会存 pre_ai 版本快照。422 MAIN_TEX_NO_DOCUMENT = 主文件没有 document 环境。
   */
  initializeManuscriptStructure(id: string): Promise<ManuscriptFileRead> {
    return request<ManuscriptFileRead>(`/manuscripts/${id}/initialize-structure`, { method: 'POST' });
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
  /** 新建文件夹（树里作为可折叠目录占位）。 */
  createManuscriptFolder(id: string, path: string): Promise<ManuscriptFileMeta> {
    return requestJson<ManuscriptFileMeta>(`/manuscripts/${id}/folders`, 'POST', { path });
  },
  /** 上传文件（含二进制）；path 缺省时后端用文件名。 */
  uploadManuscriptFile(id: string, file: File, path?: string): Promise<ManuscriptFileMeta> {
    const form = new FormData();
    form.append('file', file);
    if (path) form.append('path', path);
    return request<ManuscriptFileMeta>(`/manuscripts/${id}/files/upload`, { method: 'POST', body: form });
  },
  /** 二进制原始字节（图片/PDF 预览）：blob → objectURL 后再喂 <img>/下载。 */
  fetchManuscriptFileRaw(id: string, fid: string): Promise<Blob> {
    return requestBlob(`/manuscripts/${id}/files/${fid}/raw`);
  },

  // —— M5-B · 文件版本历史 ——
  listFileVersions(id: string, fid: string): Promise<FileVersionMeta[]> {
    return request<FileVersionMeta[]>(`/manuscripts/${id}/files/${fid}/versions`);
  },
  getFileVersion(id: string, fid: string, vid: string): Promise<FileVersionContent> {
    return request<FileVersionContent>(`/manuscripts/${id}/files/${fid}/versions/${vid}`);
  },
  /** 恢复到指定版本（当前内容自动备份为 pre_restore 快照）；readonly 文件 → 409。 */
  restoreFileVersion(id: string, fid: string, vid: string): Promise<ManuscriptFileRead> {
    return request<ManuscriptFileRead>(`/manuscripts/${id}/files/${fid}/versions/${vid}/restore`, {
      method: 'POST',
    });
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

  // —— arXiv 清洁包导出 ——
  /** 导出 arXiv 投稿清洁包 tar.gz；X-Export-Notes（| 分隔）里可能带提示。 */
  async exportManuscriptArxiv(id: string): Promise<{ blob: Blob; notes: string[] }> {
    const headers = new Headers();
    const token = getToken();
    if (token) headers.set('Authorization', `Bearer ${token}`);
    const res = await fetch(`${BASE}/manuscripts/${id}/export/arxiv`, { headers });
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
    const raw = res.headers.get('X-Export-Notes');
    const notes = raw ? raw.split('|').map((s) => s.trim()).filter(Boolean) : [];
    return { blob: await res.blob(), notes };
  },

  // —— 协作者 + 共享编辑链接 ——
  /** 按关键词搜平台用户（加协作者用）。 */
  searchUsers(q: string): Promise<UserSearchResult[]> {
    return request<UserSearchResult[]>(`/collaborators/search?q=${encodeURIComponent(q)}`);
  },
  listCollaborators(id: string): Promise<CollaboratorRead[]> {
    return request<CollaboratorRead[]>(`/manuscripts/${id}/collaborators`);
  },
  /** 加协作者（需 owner/管理员，否则 403 OWNER_OR_ADMIN_REQUIRED）；返回更新后的数组。 */
  addCollaborator(id: string, userId: string, role?: string): Promise<CollaboratorRead[]> {
    return requestJson<CollaboratorRead[]>(
      `/manuscripts/${id}/collaborators`,
      'POST',
      role ? { user_id: userId, role } : { user_id: userId },
    );
  },
  /** 移除协作者（不能删 owner，409 CANNOT_REMOVE_OWNER）。 */
  removeCollaborator(id: string, userId: string): Promise<void> {
    return request<void>(`/manuscripts/${id}/collaborators/${userId}`, { method: 'DELETE' });
  },
  /** 生成协同编辑分享链接（平台用户登录后即可加入协同）。 */
  createManuscriptShareLink(id: string, input?: { expires_days?: number; max_uses?: number }): Promise<ShareLink> {
    return requestJson<ShareLink>(`/manuscripts/${id}/share-link`, 'POST', input ?? {});
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
  testLlmModel(input: LlmTestModelInput): Promise<LlmTestResult> {
    return requestJson<LlmTestResult>('/admin/llm/test-model', 'POST', input);
  },
  getLlmUsage(opts: { projectId?: string; userId?: string; days?: number } = {}): Promise<LlmUsageRow[]> {
    const params = new URLSearchParams();
    if (opts.projectId) params.set('project_id', opts.projectId);
    if (opts.userId) params.set('user_id', opts.userId);
    if (opts.days) params.set('days', String(opts.days));
    const qs = params.toString();
    return request<LlmUsageRow[]>(`/admin/llm/usage${qs ? `?${qs}` : ''}`);
  },
  getLlmCallLogSettings(): Promise<LlmCallLogSettings> {
    return request<LlmCallLogSettings>('/admin/llm/call-logs/settings');
  },
  putLlmCallLogSettings(enabled: boolean): Promise<LlmCallLogSettings> {
    return requestJson<LlmCallLogSettings>('/admin/llm/call-logs/settings', 'PUT', { enabled });
  },
  listLlmCallLogs(opts: { limit?: number; offset?: number; stage?: string } = {}): Promise<LlmCallLogPage> {
    const params = new URLSearchParams();
    if (opts.limit) params.set('limit', String(opts.limit));
    if (opts.offset) params.set('offset', String(opts.offset));
    if (opts.stage) params.set('stage', opts.stage);
    const qs = params.toString();
    return request<LlmCallLogPage>(`/admin/llm/call-logs${qs ? `?${qs}` : ''}`);
  },
  getLlmCallLog(id: string): Promise<LlmCallLogDetail> {
    return request<LlmCallLogDetail>(`/admin/llm/call-logs/${id}`);
  },
  clearLlmCallLogs(): Promise<{ deleted: number }> {
    return request<{ deleted: number }>('/admin/llm/call-logs', { method: 'DELETE' });
  },

  // —— 我的 LLM（每个用户自管那一层，/me/llm） ——
  myLlmStatus(): Promise<LlmManagedStatus> {
    return request<LlmManagedStatus>('/me/llm/status');
  },
  llmSelfManage(): Promise<LlmManagedStatus> {
    return request<LlmManagedStatus>('/me/llm/self-manage', { method: 'POST' });
  },
  llmManaged(): Promise<LlmManagedStatus> {
    return request<LlmManagedStatus>('/me/llm/managed', { method: 'POST' });
  },
  myLlmEffective(): Promise<LlmSelfConfig> {
    return request<LlmSelfConfig>('/me/llm/effective');
  },
  myLlmProviders(): Promise<LlmProviderRead[]> {
    return request<LlmProviderRead[]>('/me/llm/providers');
  },
  createMyLlmProvider(input: LlmProviderInput): Promise<LlmProviderRead> {
    return requestJson<LlmProviderRead>('/me/llm/providers', 'POST', input);
  },
  updateMyLlmProvider(id: string, input: Partial<LlmProviderInput>): Promise<LlmProviderRead> {
    return requestJson<LlmProviderRead>(`/me/llm/providers/${id}`, 'PATCH', input);
  },
  deleteMyLlmProvider(id: string): Promise<void> {
    return request<void>(`/me/llm/providers/${id}`, { method: 'DELETE' });
  },
  myLlmRoutes(): Promise<LlmRoute[]> {
    return request<LlmRoute[]>('/me/llm/routes');
  },
  replaceMyLlmRoutes(items: LlmRoute[]): Promise<LlmRoute[]> {
    return requestJson<LlmRoute[]>('/me/llm/routes', 'PUT', items);
  },
  testMyLlmModel(input: LlmTestModelInput): Promise<LlmTestResult> {
    return requestJson<LlmTestResult>('/me/llm/test-model', 'POST', input);
  },
  testMyLlmEffective(input: { stage: string }): Promise<EffectiveTestResult> {
    return requestJson<EffectiveTestResult>('/me/llm/test-effective', 'POST', input);
  },

  // —— MCP 只读工具目录（docs/api-mcp.md） ——
  listMcpTools(): Promise<McpToolsCatalog> {
    return request<McpToolsCatalog>('/mcp/tools');
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

  // —— 我的文献库（跨研究方向的个人收藏 + 浏览记录，issue #108） ——
  listLibrary(
    opts: {
      tab: LibraryTab;
      q?: string;
      sort?: LibrarySort;
      page?: number;
      size?: number;
      year_from?: number;
      year_to?: number;
      author?: string;
      venue?: string;
    },
  ): Promise<PageOf<LibraryEntry>> {
    const params = new URLSearchParams();
    params.set('tab', opts.tab);
    if (opts.q) params.set('q', opts.q);
    if (opts.sort) params.set('sort', opts.sort);
    if (opts.page) params.set('page', String(opts.page));
    if (opts.size) params.set('size', String(opts.size));
    if (opts.year_from != null) params.set('year_from', String(opts.year_from));
    if (opts.year_to != null) params.set('year_to', String(opts.year_to));
    if (opts.author) params.set('author', opts.author);
    if (opts.venue) params.set('venue', opts.venue);
    return request<PageOf<LibraryEntry>>(`/me/library?${params.toString()}`);
  },
  /** 打开阅读页时上报一次浏览（自动建/更新浏览记录条目）。 */
  recordLibraryVisit(paperId: string): Promise<LibraryEntry> {
    return requestJson<LibraryEntry>('/me/library/visits', 'POST', { paper_id: paperId });
  },
  /** 清空浏览记录（已收藏的条目保留）。 */
  clearLibraryVisits(): Promise<void> {
    return request<void>('/me/library/visits', { method: 'DELETE' });
  },
  /** 单条详情（含 wiki 快照正文，用于源论文已删时的回退展示）。 */
  getLibraryEntry(entryId: string): Promise<LibraryEntryDetail> {
    return request<LibraryEntryDetail>(`/me/library/${entryId}`);
  },
  /** 某论文在我的文献库里的状态（是否已收藏 + 条目 id）。 */
  getLibraryState(paperId: string): Promise<LibraryState> {
    return request<LibraryState>(`/me/library/state?paper_id=${encodeURIComponent(paperId)}`);
  },
  /** 收藏进我的文献库（按论文 id 或已有条目 id）。 */
  saveToLibrary(input: { paper_id: string } | { entry_id: string }): Promise<LibraryEntry> {
    return requestJson<LibraryEntry>('/me/library', 'POST', input);
  },
  /** 移除条目：unsave=取消收藏但保留浏览记录；purge=彻底删除。 */
  removeLibraryEntry(entryId: string, mode: 'unsave' | 'purge'): Promise<void> {
    return request<void>(`/me/library/${entryId}?mode=${mode}`, { method: 'DELETE' });
  },

  // —— 我发表的（作者信息绑定 + 发表同步，issue #109） ——
  /** 我的作者绑定信息；未绑定（404 PROFILE_NOT_FOUND）时返回 null，不视为错误。 */
  async getAuthorProfile(): Promise<AuthorProfile | null> {
    try {
      return await request<AuthorProfile>('/me/author-profile');
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return null;
      throw e;
    }
  },
  saveAuthorProfile(input: AuthorProfileInput): Promise<AuthorProfile> {
    return requestJson<AuthorProfile>('/me/author-profile', 'PUT', input);
  },
  /** 触发一次文献库扫描匹配（后台任务，202）。 */
  syncPublications(): Promise<{ queued: boolean }> {
    return request<{ queued: boolean }>('/me/publications/sync', { method: 'POST' });
  },
  listPublications(
    opts: { status: PublicationStatus; page?: number; size?: number },
  ): Promise<PublicationPage> {
    const params = new URLSearchParams({ status: opts.status });
    if (opts.page) params.set('page', String(opts.page));
    if (opts.size) params.set('size', String(opts.size));
    return request<PublicationPage>(`/me/publications?${params.toString()}`);
  },
  confirmPublication(id: string): Promise<Publication> {
    return request<Publication>(`/me/publications/${id}/confirm`, { method: 'POST' });
  },
  rejectPublication(id: string): Promise<Publication> {
    return request<Publication>(`/me/publications/${id}/reject`, { method: 'POST' });
  },
  /** 手动添加发表（arxiv_id / doi / bibtex 三选一）；解析失败 422。 */
  addPublication(input: { arxiv_id: string } | { doi: string } | { bibtex: string }): Promise<Publication> {
    return requestJson<Publication>('/me/publications', 'POST', input);
  },

  // —— 每日新论文池（/daily） ——
  /** 池内现存日期（倒序）与每天篇数。 */
  listDailyDays(): Promise<DailyDay[]> {
    return request<DailyDay[]>('/daily/days');
  },
  listDailyPapers(
    opts: {
      date?: string;
      sort?: DailySort;
      page?: number;
      size?: number;
      q?: string;
      /** 类型筛选：new=新工作，cross=更新（交叉提交）；不传=全部 */
      announce?: 'new' | 'cross';
      /** 订阅分类筛选，如 cs.AI；不传=全部分类 */
      category?: string;
    } = {},
  ): Promise<DailyPage> {
    const params = new URLSearchParams();
    if (opts.date) params.set('date', opts.date);
    if (opts.sort) params.set('sort', opts.sort);
    if (opts.page) params.set('page', String(opts.page));
    if (opts.size) params.set('size', String(opts.size));
    if (opts.q) params.set('q', opts.q);
    if (opts.announce) params.set('announce', opts.announce);
    if (opts.category) params.set('category', opts.category);
    const qs = params.toString();
    return request<DailyPage>(`/daily/papers${qs ? `?${qs}` : ''}`);
  },
  getDailyPaper(entryId: string): Promise<DailyPaperDetail> {
    return request<DailyPaperDetail>(`/daily/papers/${entryId}`);
  },
  likeDailyPaper(entryId: string): Promise<DailyLikeState> {
    return request<DailyLikeState>(`/daily/papers/${entryId}/like`, { method: 'PUT' });
  },
  unlikeDailyPaper(entryId: string): Promise<DailyLikeState> {
    return request<DailyLikeState>(`/daily/papers/${entryId}/like`, { method: 'DELETE' });
  },
  /** 完整点赞名单（点赞时间倒序）。 */
  listDailyLikers(entryId: string): Promise<DailyLikerFull[]> {
    return request<DailyLikerFull[]>(`/daily/papers/${entryId}/likers`);
  },
  /** 我赞过的（随池内过期一起消失）。 */
  listMyDailyLiked(opts: { page?: number; size?: number } = {}): Promise<DailyPage> {
    const params = new URLSearchParams();
    if (opts.page) params.set('page', String(opts.page));
    if (opts.size) params.set('size', String(opts.size));
    const qs = params.toString();
    return request<DailyPage>(`/daily/liked${qs ? `?${qs}` : ''}`);
  },
  /** 批量收录：论文 × 目标（方向库 / 课题相关研究 / 个人库）。 */
  collectDaily(input: DailyCollectRequest): Promise<DailyCollectResponse> {
    return requestJson<DailyCollectResponse>('/daily/collect', 'POST', input);
  },
  getDailyCollections(entryId: string): Promise<DailyCollectionsRead> {
    return request<DailyCollectionsRead>(`/daily/papers/${entryId}/collections`);
  },
  /** 触发单篇 AI 解读编译（同步等待，约半分钟）；409 detail=COMPILE_IN_PROGRESS，502 编译失败。 */
  compileDailyPaper(entryId: string): Promise<{ entry_id: string; wiki_content: string; model: string }> {
    return request<{ entry_id: string; wiki_content: string; model: string }>(
      `/daily/papers/${entryId}/compile`,
      { method: 'POST' },
    );
  },
  getDailyCategories(): Promise<{ categories: string[] }> {
    return request<{ categories: string[] }>('/daily/categories');
  },
  /** 更新订阅分类（admin）。 */
  setDailyCategories(categories: string[]): Promise<{ categories: string[] }> {
    return requestJson<{ categories: string[] }>('/daily/categories', 'PUT', { categories });
  },
};
