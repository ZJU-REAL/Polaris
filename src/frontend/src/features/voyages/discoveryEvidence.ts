import type { HypothesisNodeRead } from '../../lib/api';

/* ============================================================
   discovery 证据卡的纯函数层（#654，设计报告 §12「支持度产品化」）：
   解析假设节点上的 grounding / novelty_report / feasibility 三块 JSON，
   算支持度、分立场分组、拼可折叠树、重建方案报告的排序。

   为什么单独抽一个无 React 的模块：
   - 支持度/排序是「数字要对」的逻辑，抽成纯函数才测得动（vitest）；
   - 三块 JSON 在 API 类型里是 unknown/Record（后端按管线约定写入，
     schema 不锁死），解析兜底集中在这里，组件层拿到的都是干净类型。
   ============================================================ */

// 与后端 services/hypothesis_pipeline.MIN_VIABLE_SCORE 一致：
// 扩展时低于它的子假设被当场自动剪枝——前端据此推断剪枝原因
export const MIN_VIABLE_SCORE = 0.15;

export type Stance = 'support' | 'refute' | 'speculation';

/** 一条子命题的文献接地（backend sanitize_grounding 的产出）。 */
export interface GroundingEntry {
  subclaim: string;
  stance: Stance;
  paperIds: string[];
  snippets: string[];
}

/** 一条子命题的查新结论（novelty_report.subclaims 的一项）。 */
export interface NoveltyEntry {
  subclaim: string;
  verdict: 'novel' | 'known' | 'uncertain';
  paperIds: string[];
  /** false = 没送 judge（如 speculation 子命题）或 judge 判不动，如实标注 */
  judged: boolean;
}

export interface FeasibilityInfo {
  signals: Record<string, unknown>;
  riskNote: string;
}

// ---- 解析（容错：后端字段乱/缺时降级，不炸组件） ----

function asStringArray(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  return raw.filter((v): v is string => typeof v === 'string' && v.length > 0);
}

/** grounding JSON → 干净条目列表；立场只认 support/refute（与后端同一口径），
    其余一律归 speculation——「有立场没证据」不该被当成有证据。 */
export function parseGrounding(raw: unknown): GroundingEntry[] {
  if (!Array.isArray(raw)) return [];
  const out: GroundingEntry[] = [];
  for (const item of raw) {
    if (typeof item !== 'object' || item === null) continue;
    const rec = item as Record<string, unknown>;
    const subclaim = typeof rec.subclaim === 'string' ? rec.subclaim.trim() : '';
    if (!subclaim) continue;
    const paperIds = asStringArray(rec.paper_ids);
    const stanceRaw = rec.stance;
    const stance: Stance =
      (stanceRaw === 'support' || stanceRaw === 'refute') && paperIds.length > 0
        ? stanceRaw
        : 'speculation';
    out.push({
      subclaim,
      stance,
      paperIds: stance === 'speculation' ? [] : paperIds,
      snippets: asStringArray(rec.snippets),
    });
  }
  return out;
}

export function parseNovelty(raw: unknown): NoveltyEntry[] {
  if (typeof raw !== 'object' || raw === null) return [];
  const subclaims = (raw as Record<string, unknown>).subclaims;
  if (!Array.isArray(subclaims)) return [];
  const out: NoveltyEntry[] = [];
  for (const item of subclaims) {
    if (typeof item !== 'object' || item === null) continue;
    const rec = item as Record<string, unknown>;
    const subclaim = typeof rec.subclaim === 'string' ? rec.subclaim.trim() : '';
    if (!subclaim) continue;
    const verdict =
      rec.verdict === 'novel' || rec.verdict === 'known' ? rec.verdict : 'uncertain';
    out.push({
      subclaim,
      verdict,
      paperIds: asStringArray(rec.paper_ids),
      judged: rec.judged === true,
    });
  }
  return out;
}

export function parseFeasibility(raw: unknown): FeasibilityInfo | null {
  if (typeof raw !== 'object' || raw === null) return null;
  const rec = raw as Record<string, unknown>;
  const signals =
    typeof rec.signals === 'object' && rec.signals !== null
      ? (rec.signals as Record<string, unknown>)
      : {};
  const riskNote = typeof rec.risk_note === 'string' ? rec.risk_note : '';
  if (Object.keys(signals).length === 0 && !riskNote) return null;
  return { signals, riskNote };
}

// ---- 支持度（§12：多少子命题真的有库内文献托底） ----

/** 支持度 = 非 speculation 且带论文引用的子命题占比。
    空 grounding 返回 null（「没有数据」和「支持度为 0」是两回事，
    界面上前者不画条、后者画一根 0% 的条）。 */
export function supportRatio(entries: GroundingEntry[]): number | null {
  if (entries.length === 0) return null;
  const backed = entries.filter(
    (e) => e.stance !== 'speculation' && e.paperIds.length > 0,
  ).length;
  return backed / entries.length;
}

export interface StanceGroups {
  support: GroundingEntry[];
  refute: GroundingEntry[];
  speculation: GroundingEntry[];
}

export function stanceGroups(entries: GroundingEntry[]): StanceGroups {
  const groups: StanceGroups = { support: [], refute: [], speculation: [] };
  for (const e of entries) groups[e.stance].push(e);
  return groups;
}

// ---- 树结构（可折叠渲染用的扁平化） ----

export interface TreeEntry {
  node: HypothesisNodeRead;
  depth: number;
  hasChildren: boolean;
}

export function childrenOf(
  nodes: HypothesisNodeRead[],
): Map<string | null, HypothesisNodeRead[]> {
  const byParent = new Map<string | null, HypothesisNodeRead[]>();
  const ids = new Set(nodes.map((n) => n.id));
  for (const n of nodes) {
    // 父不在集合里的孤儿当根挂（正常数据不会出现，防御坏数据渲染不出来）
    const key = n.parent_id !== null && ids.has(n.parent_id) ? n.parent_id : null;
    byParent.set(key, [...(byParent.get(key) ?? []), n]);
  }
  return byParent;
}

/** 平铺列表 → 先根深度序，collapsed 集合里的节点不展开子树。 */
export function flattenTree(
  nodes: HypothesisNodeRead[],
  collapsed: ReadonlySet<string>,
): TreeEntry[] {
  const byParent = childrenOf(nodes);
  const out: TreeEntry[] = [];
  const walk = (parent: string | null, depth: number) => {
    for (const n of byParent.get(parent) ?? []) {
      const hasChildren = (byParent.get(n.id) ?? []).length > 0;
      out.push({ node: n, depth, hasChildren });
      if (hasChildren && !collapsed.has(n.id)) walk(n.id, depth + 1);
    }
  };
  walk(null, 0);
  return out;
}

// ---- 方案报告的排序（与后端 discovery.summarize 同一规则就地重建） ----
// 后端把研究方案产物写在 checkpoint["artifacts"]，现有 API 不外露；
// 树端点已含存活假设 + 三块证据数据，报告页按同一排序规则重建即可，
// 不需要后端加接口（LLM 的叙述性总结文本除外，见 ReportView 的说明）。

export interface DiscoveryReport {
  /** 存活假设：score 降序（未评分排最后），同分按创建时间稳定排序 */
  alive: HypothesisNodeRead[];
  /** 被剪分支附录（§8.2 防择优汇报：放弃的路径也是产物的一部分） */
  pruned: HypothesisNodeRead[];
}

export function buildReport(nodes: HypothesisNodeRead[]): DiscoveryReport {
  const alive = nodes
    .filter((n) => n.status !== 'pruned')
    .sort((a, b) => {
      const sa = a.score ?? -1;
      const sb = b.score ?? -1;
      if (sa !== sb) return sb - sa;
      return a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0;
    });
  return { alive, pruned: nodes.filter((n) => n.status === 'pruned') };
}

// ---- 剪枝原因推断 ----
// 具体原因文本记录在后端 checkpoint 的决策留痕里（API 不外露），
// 前端按可观测信号如实分三类，文案由组件层 tr() 渲染：
// - low_score：score 低于自动剪枝阈值 → 扩展时被当场剪掉；
// - cascade：父分支被剪，随父级联；
// - decided：AI 显式判定不值得继续（细节见任务运行日志）。

export type PruneReasonKind = 'low_score' | 'cascade' | 'decided';

export function pruneReasonKind(
  node: HypothesisNodeRead,
  parentStatus: string | null,
): PruneReasonKind {
  if (node.score !== null && node.score < MIN_VIABLE_SCORE) return 'low_score';
  if (parentStatus === 'pruned') return 'cascade';
  return 'decided';
}
