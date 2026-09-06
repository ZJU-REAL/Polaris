import { useMemo, useState, type CSSProperties } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { FormField } from '../../components/ui/FormField';
import { Segmented } from '../../components/ui/Segmented';
import { toast } from '../../components/ui/Toast';
import { api, VOYAGE_TERMINAL, type HypothesisNodeRead, type VoyageRead } from '../../lib/api';
import { tr } from '../../lib/i18n';
import {
  buildReport,
  flattenTree,
  MIN_VIABLE_SCORE,
  parseFeasibility,
  parseGrounding,
  parseNovelty,
  pruneReasonKind,
  stanceGroups,
  supportRatio,
  type GroundingEntry,
  type NoveltyEntry,
  type Stance,
} from './discoveryEvidence';

/* ============================================================
   discovery（假设探索）任务的前端面（#642 → #654）：
   - 新建入口：方向文本 + 文献库 + 高级里的扩展轮数（走通用 POST /voyages）；
   - 详情页 DiscoveryPanel：树视图（可折叠、点节点看证据卡）与方案报告
     （存活假设按 score 排序 + 被剪分支附录）两种视图切换。
   证据卡的数据全部来自只读假设树 API（grounding / novelty_report /
   feasibility 随节点返回）；解析与支持度计算在 ./discoveryEvidence.ts。
   ============================================================ */

const MAX_EXPANSIONS_LIMIT = 10; // 与后端 schemas/voyage.MAX_DISCOVERY_EXPANSIONS 一致

// —— 新建入口 ——

export function DiscoveryCreateButton({ projectId }: { projectId: string | null }) {
  const [open, setOpen] = useState(false);
  const [direction, setDirection] = useState('');
  const [libraryId, setLibraryId] = useState('');
  const [maxExpansions, setMaxExpansions] = useState(3);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  // 文献库必选（#648）：四段假设管线的检索/接地边界就是这个库
  const { data: libraries } = useQuery({
    queryKey: ['libraries', 'all'],
    queryFn: () => api.listLibraries(),
    enabled: open,
  });

  const createMutation = useMutation({
    mutationFn: () =>
      api.createVoyage({
        kind: 'discovery',
        project_id: projectId!,
        goal: direction.trim(),
        params: { direction: direction.trim(), max_expansions: maxExpansions, library_id: libraryId },
      }),
    onSuccess: (run) => {
      setOpen(false);
      setDirection('');
      void queryClient.invalidateQueries({ queryKey: ['voyages'] });
      navigate(`/voyages/${run.id}`);
    },
    onError: (e) =>
      toast(`${tr('创建失败：', 'Create failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const canSubmit = !!projectId && direction.trim().length > 0 && !!libraryId && !createMutation.isPending;
  return (
    <>
      <button
        className="btn btn-soft"
        disabled={!projectId}
        title={projectId ? undefined : tr('先选择一个课题', 'Pick a topic first')}
        onClick={() => setOpen(true)}
      >
        <Icon name="compass" size={13} />
        {tr('新建假设探索', 'New hypothesis discovery')}
      </button>
      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title={tr('新建假设探索任务', 'New hypothesis discovery task')}
        sub={tr(
          'AI 会围绕这个方向生成假设树：提出根假设，逐轮挑最有希望的分支展开，最后汇总整棵树。',
          'The AI grows a hypothesis tree around this direction: seed a root hypothesis, expand the most promising branch each round, then summarize the whole tree.',
        )}
        footer={
          <div className="row gap8" style={{ justifyContent: 'flex-end' }}>
            <button className="btn btn-ghost" onClick={() => setOpen(false)}>
              {tr('取消', 'Cancel')}
            </button>
            <button className="btn btn-primary" disabled={!canSubmit} onClick={() => createMutation.mutate()}>
              {createMutation.isPending ? tr('创建中…', 'Creating…') : tr('开始探索', 'Start exploring')}
            </button>
          </div>
        }
      >
        <div style={{ padding: 20 }}>
          <FormField
            label="研究方向"
            en="Research direction"
            hint={tr('一句话说清想探索什么，例如：LLM agent 的长期记忆机制', 'One sentence on what to explore, e.g. long-term memory for LLM agents')}
          >
            <textarea
              className="textarea"
              rows={3}
              value={direction}
              onChange={(e) => setDirection(e.target.value)}
              placeholder={tr('想探索的研究方向…', 'Direction to explore…')}
            />
          </FormField>
          <FormField
            label="文献库"
            en="Library"
            hint={tr('假设的文献接地与查新都在这个库里检索', 'Grounding and novelty checks retrieve from this library')}
            style={{ marginTop: 10 }}
          >
            <select
              className="input"
              value={libraryId}
              onChange={(e) => setLibraryId(e.target.value)}
            >
              <option value="">{tr('选择文献库…', 'Pick a library…')}</option>
              {(libraries ?? []).map((lib) => (
                <option key={lib.id} value={lib.id}>
                  {lib.name}（{lib.paper_count} {tr('篇', 'papers')}）
                </option>
              ))}
            </select>
          </FormField>
          <details style={{ marginTop: 12 }}>
            <summary style={{ fontSize: 12, color: 'var(--text-3)', cursor: 'pointer', userSelect: 'none' }}>
              {tr('高级选项', 'Advanced')}
            </summary>
            <FormField
              label="扩展轮数"
              en="Expansion rounds"
              hint={tr(`每轮对最有希望的假设展开 2-3 个子假设（0-${MAX_EXPANSIONS_LIMIT}）`, `Each round expands the most promising hypothesis into 2-3 children (0-${MAX_EXPANSIONS_LIMIT})`)}
              style={{ marginTop: 10 }}
            >
              <input
                className="input"
                type="number"
                min={0}
                max={MAX_EXPANSIONS_LIMIT}
                value={maxExpansions}
                onChange={(e) => {
                  const v = Number(e.target.value);
                  if (Number.isInteger(v)) setMaxExpansions(Math.max(0, Math.min(MAX_EXPANSIONS_LIMIT, v)));
                }}
                style={{ width: 120 }}
              />
            </FormField>
          </details>
        </div>
      </Modal>
    </>
  );
}

// —— 文案映射（模块级常量保留 {zh,en}，渲染处再 tr()，语言切换才生效） ——

const OPEN_META = { zh: '待探索', en: 'Open', bg: 'var(--accent-soft)', tx: 'var(--accent-text)' };
const NODE_STATUS_META: Record<string, { zh: string; en: string; bg: string; tx: string }> = {
  open: OPEN_META,
  expanded: { zh: '已扩展', en: 'Expanded', bg: 'var(--info-bg)', tx: 'var(--info-tx)' },
  pruned: { zh: '已剪枝', en: 'Pruned', bg: 'var(--surface-3)', tx: 'var(--text-3)' },
  validated: { zh: '已验证', en: 'Validated', bg: 'var(--ok-bg)', tx: 'var(--ok-tx)' },
  refuted: { zh: '已否证', en: 'Refuted', bg: 'var(--danger-bg)', tx: 'var(--danger-tx)' },
};

const STANCE_META: Record<Stance, { zh: string; en: string; color: string }> = {
  support: { zh: '支持', en: 'Supports', color: 'var(--ok)' },
  refute: { zh: '反驳', en: 'Refutes', color: 'var(--danger)' },
  speculation: { zh: '推测（库内没检到证据）', en: 'Speculation (no in-library evidence)', color: 'var(--text-3)' },
};

const VERDICT_META: Record<NoveltyEntry['verdict'], { zh: string; en: string; bg: string; tx: string }> = {
  novel: { zh: '新颖', en: 'Novel', bg: 'var(--ok-bg)', tx: 'var(--ok-tx)' },
  known: { zh: '已有研究', en: 'Known', bg: 'var(--surface-3)', tx: 'var(--text-3)' },
  uncertain: { zh: '不确定', en: 'Uncertain', bg: 'var(--warn-bg)', tx: 'var(--warn-tx)' },
};

/** 可行性信号的键名翻译（后端 hypothesis_pipeline.feasibility 的确定性信号；
    未知键按原名展示，后端加信号前端不至于瞎）。 */
const SIGNAL_LABELS: Record<string, { zh: string; en: string }> = {
  grounded_paper_count: { zh: '接地论文数', en: 'Grounded papers' },
  venues: { zh: '来源分布', en: 'Venues' },
  year_range: { zh: '年份范围', en: 'Year range' },
  related_chunk_hits: { zh: '库内相关片段数', en: 'Related chunks in library' },
};

/** 剪枝原因的界面文案：具体原因文本在后端 checkpoint 里、API 不外露，
    前端按可观测信号（分数阈值 / 父状态）如实推断三类，不编细节。 */
function pruneReasonText(node: HypothesisNodeRead, parentStatus: string | null): string {
  const kind = pruneReasonKind(node, parentStatus);
  if (kind === 'low_score') {
    return tr(
      `评分 ${node.score!.toFixed(2)} 低于阈值 ${MIN_VIABLE_SCORE}，扩展时被自动放弃`,
      `Score ${node.score!.toFixed(2)} fell below the ${MIN_VIABLE_SCORE} threshold — auto-pruned during expansion`,
    );
  }
  if (kind === 'cascade') return tr('父分支被放弃后随之放弃', 'Dropped along with its parent branch');
  return tr('AI 判定不值得继续（细节见运行日志）', 'The AI judged it not worth pursuing (see run logs)');
}

// —— 论文标题解析（证据卡里的引用要能点着标题跳论文） ——

// 一次要解析的标题上限：超出的引用退化为短 id 展示，防极端大树一次打爆请求
const PAPER_TITLE_FETCH_CAP = 40;

/** paper_ids → 标题映射。逐篇走 ['paper', id]（与全站论文详情同一 queryKey，
    读过的论文直接命中缓存）；现有 API 没有按 id 批量取标题的端点，如果证据卡
    引用规模上去了，值得让后端加一个批量标题接口再换掉这里。 */
function usePaperTitles(paperIds: string[]): Map<string, string> {
  const results = useQueries({
    queries: paperIds.slice(0, PAPER_TITLE_FETCH_CAP).map((id) => ({
      queryKey: ['paper', id],
      queryFn: () => api.getPaper(id),
      staleTime: 10 * 60_000,
      retry: false,
    })),
  });
  const map = new Map<string, string>();
  results.forEach((r, i) => {
    const id = paperIds[i];
    if (id && r.data?.title) map.set(id, r.data.title);
  });
  return map;
}

function PaperChips({ ids, titles }: { ids: string[]; titles: Map<string, string> }) {
  const navigate = useNavigate();
  if (ids.length === 0) return null;
  return (
    <span className="row" style={{ gap: 4, flexWrap: 'wrap' }}>
      {ids.map((id) => (
        <button
          key={id}
          type="button"
          className="pill sm"
          title={tr('打开论文', 'Open paper')}
          onClick={() => navigate(`/papers/${id}/read`)}
          style={{
            background: 'var(--surface-2)',
            color: 'var(--accent-text)',
            border: '0.5px solid var(--border-2)',
            cursor: 'pointer',
            maxWidth: 280,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            display: 'inline-block',
          }}
        >
          <Icon name="book" size={10} style={{ marginRight: 3, verticalAlign: -1 }} />
          {titles.get(id) ?? `${id.slice(0, 8)}…`}
        </button>
      ))}
    </span>
  );
}

// —— 支持度条（§12：非推测且有论文引用的子命题占比） ——

function SupportBar({ ratio, style }: { ratio: number | null; style?: CSSProperties }) {
  if (ratio === null) return null;
  const pct = Math.round(ratio * 100);
  return (
    <div style={style} title={tr('支持度 = 有库内论文托底（非推测）的子命题占比', 'Support = share of subclaims backed by in-library papers (non-speculation)')}>
      <div className="row" style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 3 }}>
        <span>{tr('文献支持度', 'Literature support')}</span>
        <span className="mono" style={{ marginLeft: 'auto' }}>{pct}%</span>
      </div>
      <div style={{ height: 6, borderRadius: 3, background: 'var(--surface-3)', overflow: 'hidden' }}>
        <div style={{ width: `${pct}%`, height: '100%', borderRadius: 3, background: 'var(--ok)' }} />
      </div>
    </div>
  );
}

// —— 节点证据卡面板（三组证据 + 支持度 + 查新 + 可行性） ——

const CLAMP3: CSSProperties = {
  display: '-webkit-box',
  WebkitLineClamp: 3,
  WebkitBoxOrient: 'vertical',
  overflow: 'hidden',
};

function fmtSignal(v: unknown): string {
  if (v === null || v === undefined) return '—';
  if (Array.isArray(v)) return v.length === 0 ? '—' : v.join(' – ');
  if (typeof v === 'object') {
    const entries = Object.entries(v as Record<string, unknown>);
    return entries.length === 0 ? '—' : entries.map(([k, n]) => `${k} ×${String(n)}`).join(' · ');
  }
  return String(v);
}

function EvidenceGroup({ stance, entries, titles }: { stance: Stance; entries: GroundingEntry[]; titles: Map<string, string> }) {
  if (entries.length === 0) return null;
  const meta = STANCE_META[stance];
  return (
    <div style={{ marginTop: 10 }}>
      <div className="row gap6" style={{ fontSize: 11.5, fontWeight: 650, color: meta.color, marginBottom: 4 }}>
        {tr(meta.zh, meta.en)}
        <span className="mono" style={{ fontWeight: 400, color: 'var(--text-4)' }}>×{entries.length}</span>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {entries.map((e, i) => (
          <div key={i} style={{ borderLeft: `2px solid ${meta.color}`, paddingLeft: 10 }}>
            <div style={{ fontSize: 12.5, color: 'var(--text-1)' }}>{e.subclaim}</div>
            {e.paperIds.length > 0 && (
              <div style={{ marginTop: 4 }}>
                <PaperChips ids={e.paperIds} titles={titles} />
              </div>
            )}
            {e.snippets.map((s, j) => (
              <div
                key={j}
                title={s}
                style={{
                  ...CLAMP3,
                  marginTop: 4,
                  fontSize: 11.5,
                  lineHeight: 1.55,
                  color: 'var(--text-3)',
                  background: 'var(--surface-2)',
                  borderRadius: 6,
                  padding: '5px 8px',
                }}
              >
                {s}
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

function NodeDetailPanel({ node, pruneReason, style }: { node: HypothesisNodeRead; pruneReason: string | null; style?: CSSProperties }) {
  const grounding = useMemo(() => parseGrounding(node.grounding), [node.grounding]);
  const novelty = useMemo(() => parseNovelty(node.novelty_report), [node.novelty_report]);
  const feas = useMemo(() => parseFeasibility(node.feasibility), [node.feasibility]);
  const groups = stanceGroups(grounding);
  const ratio = supportRatio(grounding);
  // 引用到的论文去重后解析标题（接地 + 查新两处都可能引用）
  const paperIds = useMemo(() => {
    const seen = new Set<string>();
    const out: string[] = [];
    for (const list of [...grounding.map((e) => e.paperIds), ...novelty.map((e) => e.paperIds)]) {
      for (const id of list) {
        if (!seen.has(id)) {
          seen.add(id);
          out.push(id);
        }
      }
    }
    return out;
  }, [grounding, novelty]);
  const titles = usePaperTitles(paperIds);

  const empty = grounding.length === 0 && novelty.length === 0 && !feas;
  return (
    <div
      style={{
        background: 'var(--surface-1)',
        border: '0.5px solid var(--border-2)',
        borderRadius: 10,
        padding: '10px 14px 12px',
        ...style,
      }}
    >
      {pruneReason && (
        <div className="row gap6" style={{ fontSize: 12, color: 'var(--danger-tx)', marginBottom: 8 }}>
          <Icon name="x" size={12} />
          {tr('剪枝原因：', 'Pruned because: ')}
          {pruneReason}
        </div>
      )}
      {empty ? (
        <div style={{ fontSize: 12, color: 'var(--text-3)' }}>
          {tr(
            '这个节点还没有证据数据（根假设与待扩展节点不跑接地管线）。',
            'No evidence data on this node yet (root and unexpanded nodes skip the grounding pipeline).',
          )}
        </div>
      ) : (
        <>
          <SupportBar ratio={ratio} />
          {/* 证据三组：支持 / 反驳 / 推测 */}
          <EvidenceGroup stance="support" entries={groups.support} titles={titles} />
          <EvidenceGroup stance="refute" entries={groups.refute} titles={titles} />
          <EvidenceGroup stance="speculation" entries={groups.speculation} titles={titles} />
          {/* 查新结论：逐子命题 verdict */}
          {novelty.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <div style={{ fontSize: 11.5, fontWeight: 650, color: 'var(--text-2)', marginBottom: 4 }}>
                {tr('查新结论', 'Novelty check')}
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
                {novelty.map((e, i) => {
                  const m = VERDICT_META[e.verdict];
                  return (
                    <div key={i} className="row gap6" style={{ alignItems: 'flex-start' }}>
                      <span className="pill sm" style={{ background: m.bg, color: m.tx, flexShrink: 0 }}>
                        {tr(m.zh, m.en)}
                      </span>
                      <span style={{ fontSize: 12.5, minWidth: 0 }}>
                        {e.subclaim}
                        {!e.judged && (
                          <span style={{ fontSize: 11, color: 'var(--text-4)', marginLeft: 6 }}>
                            {tr('（未送查新判定）', '(not judged)')}
                          </span>
                        )}
                      </span>
                      {e.paperIds.length > 0 && (
                        <span style={{ marginLeft: 'auto', flexShrink: 0 }}>
                          <PaperChips ids={e.paperIds} titles={titles} />
                        </span>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
          {/* 可行性：确定性信号小表 + 风险论证 */}
          {feas && (
            <div style={{ marginTop: 12 }}>
              <div style={{ fontSize: 11.5, fontWeight: 650, color: 'var(--text-2)', marginBottom: 4 }}>
                {tr('可行性', 'Feasibility')}
              </div>
              {Object.keys(feas.signals).length > 0 && (
                <div style={{ display: 'grid', gridTemplateColumns: 'max-content 1fr', gap: '3px 14px', fontSize: 12 }}>
                  {Object.entries(feas.signals).map(([k, v]) => (
                    <span key={k} style={{ display: 'contents' }}>
                      <span style={{ color: 'var(--text-3)' }}>
                        {SIGNAL_LABELS[k] ? tr(SIGNAL_LABELS[k].zh, SIGNAL_LABELS[k].en) : k}
                      </span>
                      <span className="mono" style={{ color: 'var(--text-2)', wordBreak: 'break-word' }}>{fmtSignal(v)}</span>
                    </span>
                  ))}
                </div>
              )}
              {feas.riskNote && (
                <div style={{ fontSize: 12, color: 'var(--text-2)', lineHeight: 1.6, marginTop: 6 }}>
                  <span style={{ color: 'var(--text-3)' }}>{tr('风险论证：', 'Risk note: ')}</span>
                  {feas.riskNote}
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

// —— 树视图（可折叠 + 点节点展开证据卡） ——

function TreeView({ nodes }: { nodes: HypothesisNodeRead[] }) {
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set());
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const entries = useMemo(() => flattenTree(nodes, collapsed), [nodes, collapsed]);
  const statusById = useMemo(() => new Map(nodes.map((n) => [n.id, n.status])), [nodes]);

  const toggleCollapse = (id: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      {entries.map(({ node, depth, hasChildren }) => {
        const m = NODE_STATUS_META[node.status] ?? OPEN_META;
        const pruned = node.status === 'pruned';
        const reason = pruned
          ? pruneReasonText(node, node.parent_id ? statusById.get(node.parent_id) ?? null : null)
          : null;
        const ratio = supportRatio(parseGrounding(node.grounding));
        const selected = node.id === selectedId;
        const isCollapsed = collapsed.has(node.id);
        return (
          <div key={node.id}>
            <div
              className="row gap6"
              onClick={() => setSelectedId(selected ? null : node.id)}
              // 剪枝原因 hover 即见；点开证据卡里还有一份
              title={reason ? `${node.statement}\n${tr('剪枝原因：', 'Pruned because: ')}${reason}` : node.statement}
              style={{
                alignItems: 'center',
                cursor: 'pointer',
                borderRadius: 7,
                padding: '3px 6px',
                paddingLeft: 6 + depth * 16,
                background: selected ? 'var(--surface-2)' : undefined,
              }}
            >
              {hasChildren ? (
                <button
                  type="button"
                  onClick={(ev) => {
                    ev.stopPropagation();
                    toggleCollapse(node.id);
                  }}
                  title={isCollapsed ? tr('展开子假设', 'Expand children') : tr('收起子假设', 'Collapse children')}
                  style={{
                    border: 'none',
                    background: 'none',
                    padding: 0,
                    width: 18,
                    height: 18,
                    display: 'inline-flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    cursor: 'pointer',
                    color: 'var(--text-3)',
                    flexShrink: 0,
                  }}
                >
                  <Icon
                    name="chevron"
                    size={11}
                    style={{ transform: isCollapsed ? 'none' : 'rotate(90deg)', transition: 'transform .15s' }}
                  />
                </button>
              ) : (
                <span style={{ width: 18, flexShrink: 0 }} />
              )}
              <span className="pill sm" style={{ background: m.bg, color: m.tx, flexShrink: 0 }}>
                {tr(m.zh, m.en)}
              </span>
              <span
                style={{
                  fontSize: 12.5,
                  flex: 1,
                  minWidth: 0,
                  whiteSpace: 'nowrap',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  textDecoration: pruned ? 'line-through' : undefined,
                  color: pruned ? 'var(--text-3)' : undefined,
                }}
              >
                {node.statement}
              </span>
              <span className="row gap8" style={{ flexShrink: 0, alignItems: 'center' }}>
                {ratio !== null && (
                  <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>
                    {tr('支持', 'sup')} {Math.round(ratio * 100)}%
                  </span>
                )}
                {node.score != null && (
                  <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>
                    {node.score.toFixed(2)}
                  </span>
                )}
              </span>
            </div>
            {selected && (
              <NodeDetailPanel
                node={node}
                pruneReason={reason}
                style={{ margin: `6px 0 8px ${6 + depth * 16 + 18}px` }}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

// —— 方案报告视图 ——
// 后端 discovery.summarize 把研究方案产物写在 run.checkpoint["artifacts"]，
// 现有 API 不外露；树端点已含证据卡全部结构化数据，这里按后端同一排序规则
// （score 降序、未评分垫底、同分按创建时间）就地重建报告——任务没跑完也能
// 预览当前排序。LLM 的叙述性总结文本只存在于产物里，等后端开放产物读取
// 接口后再补一段「AI 总结」。

function ReportView({ nodes, active }: { nodes: HypothesisNodeRead[]; active: boolean }) {
  const { alive, pruned } = useMemo(() => buildReport(nodes), [nodes]);
  const statusById = useMemo(() => new Map(nodes.map((n) => [n.id, n.status])), [nodes]);
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      {active && (
        <div style={{ fontSize: 12, color: 'var(--text-3)' }}>
          {tr('任务还在进行中：以下是当前假设树的即时排序，跑完后会稳定下来。', 'The run is still in progress — this is a live ranking of the current tree; it settles once the run finishes.')}
        </div>
      )}
      {alive.length === 0 && (
        <div style={{ fontSize: 12.5, color: 'var(--text-3)' }}>
          {tr('没有存活的假设（全部被剪枝）。', 'No surviving hypotheses — every branch was pruned.')}
        </div>
      )}
      {alive.map((n, i) => {
        const m = NODE_STATUS_META[n.status] ?? OPEN_META;
        const grounding = parseGrounding(n.grounding);
        const groups = stanceGroups(grounding);
        const ratio = supportRatio(grounding);
        return (
          <div key={n.id} style={{ border: '0.5px solid var(--border-2)', borderRadius: 10, padding: '12px 14px' }}>
            <div className="row gap8" style={{ alignItems: 'flex-start' }}>
              <span className="mono" style={{ fontSize: 12, color: 'var(--text-4)', flexShrink: 0, paddingTop: 1 }}>
                #{i + 1}
              </span>
              <span style={{ fontSize: 13.5, fontWeight: 620, flex: 1, minWidth: 0 }}>{n.statement}</span>
              <span className="row gap6" style={{ flexShrink: 0, alignItems: 'center' }}>
                <span className="pill sm" style={{ background: m.bg, color: m.tx }}>{tr(m.zh, m.en)}</span>
                {n.score != null && (
                  <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>{n.score.toFixed(2)}</span>
                )}
              </span>
            </div>
            <SupportBar ratio={ratio} style={{ marginTop: 10 }} />
            {/* 三组证据摘要：只列子命题；引用与原文片段在下面的完整证据卡里 */}
            {(['support', 'refute', 'speculation'] as const).map(
              (k) =>
                groups[k].length > 0 && (
                  <div key={k} style={{ marginTop: 8 }}>
                    <div style={{ fontSize: 11, fontWeight: 650, color: STANCE_META[k].color, marginBottom: 2 }}>
                      {tr(STANCE_META[k].zh, STANCE_META[k].en)}（{groups[k].length}）
                    </div>
                    {groups[k].map((e, j) => (
                      <div key={j} style={{ fontSize: 12, color: 'var(--text-2)', lineHeight: 1.6 }}>
                        · {e.subclaim}
                      </div>
                    ))}
                  </div>
                ),
            )}
            {(grounding.length > 0 || n.novelty_report || n.feasibility) && (
              <details style={{ marginTop: 10 }}>
                <summary style={{ fontSize: 12, color: 'var(--accent-text)', cursor: 'pointer', userSelect: 'none' }}>
                  {tr('查看完整证据卡（引用论文、原文片段、查新、可行性）', 'Full evidence card (papers, snippets, novelty, feasibility)')}
                </summary>
                <NodeDetailPanel node={n} pruneReason={null} style={{ marginTop: 8 }} />
              </details>
            )}
          </div>
        );
      })}
      {/* 被剪分支附录：§8.2 防择优汇报——放弃的路径与原因也是方案的一部分 */}
      {pruned.length > 0 && (
        <details style={{ border: '0.5px solid var(--border-2)', borderRadius: 10, padding: '10px 14px' }}>
          <summary style={{ fontSize: 12.5, fontWeight: 650, cursor: 'pointer', userSelect: 'none', color: 'var(--text-2)' }}>
            {tr(`被放弃的分支附录（${pruned.length}）`, `Pruned branches appendix (${pruned.length})`)}
          </summary>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 8 }}>
            {pruned.map((n) => (
              <div key={n.id}>
                <div style={{ fontSize: 12.5, color: 'var(--text-2)' }}>{n.statement}</div>
                <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 1 }}>
                  {n.score != null && <span className="mono">score {n.score.toFixed(2)} · </span>}
                  {pruneReasonText(n, n.parent_id ? statusById.get(n.parent_id) ?? null : null)}
                </div>
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

// —— 详情页入口卡：树视图 / 方案报告 切换 ——

export function DiscoveryPanel({ voyage }: { voyage: VoyageRead }) {
  const active = !VOYAGE_TERMINAL.has(voyage.status);
  const [view, setView] = useState<'tree' | 'report'>('tree');
  const { data } = useQuery({
    queryKey: ['hypothesis-tree', voyage.id],
    queryFn: () => api.listHypothesisTree(voyage.id),
    retry: false,
    // 任务还在跑就轮询：树是逐轮长出来的
    refetchInterval: active ? 10_000 : false,
  });
  const nodes = data ?? [];

  return (
    <div className="card card-pad" style={{ marginBottom: 20 }}>
      <div className="row" style={{ marginBottom: 10, flexWrap: 'wrap', gap: 8 }}>
        <span className="section-h">
          <Icon name="compass" size={15} style={{ color: 'var(--accent)' }} />
          {tr('假设树', 'Hypothesis tree')}
        </span>
        {nodes.length > 0 && (
          <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>
            {nodes.length} {tr('个节点', 'nodes')}
          </span>
        )}
        <span style={{ marginLeft: 'auto' }}>
          <Segmented
            options={[
              { v: 'tree' as const, label: tr('树视图', 'Tree') },
              { v: 'report' as const, label: tr('方案报告', 'Report') },
            ]}
            value={view}
            onChange={setView}
          />
        </span>
      </div>
      {nodes.length === 0 ? (
        <div style={{ fontSize: 12.5, color: 'var(--text-3)' }}>
          {active
            ? tr('树还没长出来：AI 正在生成根假设…', 'No tree yet — the AI is seeding the root hypothesis…')
            : tr('这次任务没有留下假设树。', 'This run left no hypothesis tree.')}
        </div>
      ) : view === 'tree' ? (
        <TreeView nodes={nodes} />
      ) : (
        <ReportView nodes={nodes} active={active} />
      )}
    </div>
  );
}
