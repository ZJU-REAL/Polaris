/* ============================================================
   discovery 过程记录（披露产物）的纯函数层（#655，设计报告 §8.2）。

   后端 summarize 时把「实际做了什么」确定性归档成
   artifacts["discovery-disclosure.json"]（检索全录 / 读过 vs 引用的论文 /
   分支时间线与真实剪枝原因 / 警告），经 GET /voyages/{id}/artifacts/{name}
   取回。这里做两件事：
   - 容错解析成干净类型（产物 schema 不锁死，字段乱/缺时降级不炸组件）；
   - 视图数据变换（按阶段分组、剪枝原因反查表）——纯函数，vitest 直测。
   ============================================================ */

export type DisclosurePhase = 'generate' | 'ground' | 'novelty' | 'other';

/** 一次检索：哪一轮、哪个阶段、为哪个节点、查了什么、捞回哪些论文。 */
export interface DisclosureQuery {
  round: number | null;
  phase: DisclosurePhase;
  nodeId: string | null;
  query: string;
  paperIds: string[];
}

export interface DisclosureTimelineEvent {
  event: 'created' | 'expanded' | 'pruned';
  round: number | null;
  /** pruned 事件才有：后端决策留痕里的原话（数据，不做翻译） */
  reason: string | null;
  /** 级联剪枝的来源节点（有它说明本节点是随父被剪的） */
  cascadeFrom: string | null;
}

export interface DisclosureBranch {
  nodeId: string;
  parentId: string | null;
  statement: string;
  status: string;
  score: number | null;
  timeline: DisclosureTimelineEvent[];
}

export interface DisclosureWarning {
  code: string;
}

export interface DisclosureData {
  queries: DisclosureQuery[];
  papers: {
    retrieved: string[];
    cited: string[];
    retrievedNotCited: string[];
    citedNotRetrieved: string[];
  };
  branches: DisclosureBranch[];
  warnings: DisclosureWarning[];
  /** 全 run token 总量（accounting.total；缺失为 null） */
  totalTokens: { prompt: number; completion: number } | null;
}

// ---- 解析 ----

function asStringArray(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  return raw.filter((v): v is string => typeof v === 'string' && v.length > 0);
}

function asRound(raw: unknown): number | null {
  return typeof raw === 'number' && Number.isFinite(raw) ? raw : null;
}

function parseQuery(raw: unknown): DisclosureQuery | null {
  if (typeof raw !== 'object' || raw === null) return null;
  const rec = raw as Record<string, unknown>;
  const query = typeof rec.query === 'string' ? rec.query : '';
  if (!query) return null;
  const phase =
    rec.phase === 'generate' || rec.phase === 'ground' || rec.phase === 'novelty'
      ? rec.phase
      : 'other';
  return {
    round: asRound(rec.round),
    phase,
    nodeId: typeof rec.node_id === 'string' ? rec.node_id : null,
    query,
    paperIds: asStringArray(rec.paper_ids),
  };
}

function parseTimelineEvent(raw: unknown): DisclosureTimelineEvent | null {
  if (typeof raw !== 'object' || raw === null) return null;
  const rec = raw as Record<string, unknown>;
  if (rec.event !== 'created' && rec.event !== 'expanded' && rec.event !== 'pruned') return null;
  return {
    event: rec.event,
    round: asRound(rec.round),
    reason: typeof rec.reason === 'string' && rec.reason ? rec.reason : null,
    cascadeFrom: typeof rec.cascade_from === 'string' ? rec.cascade_from : null,
  };
}

function parseBranch(raw: unknown): DisclosureBranch | null {
  if (typeof raw !== 'object' || raw === null) return null;
  const rec = raw as Record<string, unknown>;
  if (typeof rec.node_id !== 'string' || !rec.node_id) return null;
  return {
    nodeId: rec.node_id,
    parentId: typeof rec.parent_id === 'string' ? rec.parent_id : null,
    statement: typeof rec.statement === 'string' ? rec.statement : '',
    status: typeof rec.status === 'string' ? rec.status : '',
    score: typeof rec.score === 'number' ? rec.score : null,
    timeline: Array.isArray(rec.timeline)
      ? rec.timeline.map(parseTimelineEvent).filter((e): e is DisclosureTimelineEvent => e !== null)
      : [],
  };
}

/** 产物 JSON → 干净结构；根本不是对象（旧产物/坏数据）返回 null，组件降级。 */
export function parseDisclosure(raw: unknown): DisclosureData | null {
  if (typeof raw !== 'object' || raw === null) return null;
  const rec = raw as Record<string, unknown>;
  const papersRaw =
    typeof rec.papers === 'object' && rec.papers !== null
      ? (rec.papers as Record<string, unknown>)
      : {};
  const accounting =
    typeof rec.accounting === 'object' && rec.accounting !== null
      ? (rec.accounting as Record<string, unknown>)
      : {};
  const total =
    typeof accounting.total === 'object' && accounting.total !== null
      ? (accounting.total as Record<string, unknown>)
      : null;
  return {
    queries: Array.isArray(rec.queries)
      ? rec.queries.map(parseQuery).filter((q): q is DisclosureQuery => q !== null)
      : [],
    papers: {
      retrieved: asStringArray(papersRaw.retrieved),
      cited: asStringArray(papersRaw.cited),
      retrievedNotCited: asStringArray(papersRaw.retrieved_not_cited),
      citedNotRetrieved: asStringArray(papersRaw.cited_not_retrieved),
    },
    branches: Array.isArray(rec.branches)
      ? rec.branches.map(parseBranch).filter((b): b is DisclosureBranch => b !== null)
      : [],
    warnings: Array.isArray(rec.warnings)
      ? rec.warnings
          .filter(
            (w): w is Record<string, unknown> =>
              typeof w === 'object' && w !== null && typeof (w as Record<string, unknown>).code === 'string',
          )
          .map((w) => ({ code: String(w.code) }))
      : [],
    totalTokens: total
      ? {
          prompt: typeof total.prompt_tokens === 'number' ? total.prompt_tokens : 0,
          completion: typeof total.completion_tokens === 'number' ? total.completion_tokens : 0,
        }
      : null,
  };
}

// ---- 视图变换 ----

const PHASE_ORDER: DisclosurePhase[] = ['generate', 'ground', 'novelty', 'other'];

export interface PhaseGroup {
  phase: DisclosurePhase;
  items: DisclosureQuery[];
}

/** 查询按阶段分组（找灵感 → 找证据 → 查新），空阶段不出现；组内保持原始
    （轮次内时间）顺序。 */
export function queriesByPhase(queries: DisclosureQuery[]): PhaseGroup[] {
  const byPhase = new Map<DisclosurePhase, DisclosureQuery[]>();
  for (const q of queries) byPhase.set(q.phase, [...(byPhase.get(q.phase) ?? []), q]);
  return PHASE_ORDER.filter((p) => byPhase.has(p)).map((p) => ({ phase: p, items: byPhase.get(p)! }));
}

export interface PruneRecord {
  /** 决策留痕里的原话；级联节点没有原话，reason 为 null、cascadeFrom 指向来源 */
  reason: string | null;
  cascadeFrom: string | null;
  round: number | null;
}

/** 剪枝原因反查表（node_id → 真实原因）：树视图/报告视图优先用它，
    没有产物（run 未跑完 / 旧版本 run）时再退回 D5 的推断逻辑。 */
export function pruneRecordsOf(disclosure: DisclosureData): Map<string, PruneRecord> {
  const out = new Map<string, PruneRecord>();
  for (const b of disclosure.branches) {
    const pruned = b.timeline.find((e) => e.event === 'pruned');
    if (pruned) {
      out.set(b.nodeId, { reason: pruned.reason, cascadeFrom: pruned.cascadeFrom, round: pruned.round });
    }
  }
  return out;
}

/** 被剪分支列表（时间线视图用）：保持产物顺序（＝建树顺序）。 */
export function prunedBranches(disclosure: DisclosureData): DisclosureBranch[] {
  return disclosure.branches.filter((b) => b.status === 'pruned');
}
