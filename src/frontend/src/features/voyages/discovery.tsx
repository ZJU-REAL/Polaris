import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { FormField } from '../../components/ui/FormField';
import { toast } from '../../components/ui/Toast';
import { api, VOYAGE_TERMINAL, type HypothesisNodeRead, type VoyageRead } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   discovery（假设探索）任务的最小前端面（#642）：
   - 新建入口：方向文本 + 高级里的扩展轮数（走通用 POST /voyages）；
   - 详情页的节点列表卡：挂只读假设树 API，按父子缩进平铺展示。
   树的图形化可视化归后续里程碑（D5），这里只保证「建得了、看得见」。
   ============================================================ */

const MAX_EXPANSIONS_LIMIT = 10; // 与后端 schemas/voyage.MAX_DISCOVERY_EXPANSIONS 一致

// —— 新建入口 ——

export function DiscoveryCreateButton({ projectId }: { projectId: string | null }) {
  const [open, setOpen] = useState(false);
  const [direction, setDirection] = useState('');
  const [maxExpansions, setMaxExpansions] = useState(3);
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const createMutation = useMutation({
    mutationFn: () =>
      api.createVoyage({
        kind: 'discovery',
        project_id: projectId!,
        goal: direction.trim(),
        params: { direction: direction.trim(), max_expansions: maxExpansions },
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

  const canSubmit = !!projectId && direction.trim().length > 0 && !createMutation.isPending;
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

// —— 详情页：假设树节点列表卡 ——

const OPEN_META = { zh: '待探索', en: 'Open', bg: 'var(--accent-soft)', tx: 'var(--accent-text)' };
const NODE_STATUS_META: Record<string, { zh: string; en: string; bg: string; tx: string }> = {
  open: OPEN_META,
  expanded: { zh: '已扩展', en: 'Expanded', bg: 'var(--info-bg)', tx: 'var(--info-tx)' },
  pruned: { zh: '已剪枝', en: 'Pruned', bg: 'var(--surface-3)', tx: 'var(--text-3)' },
  validated: { zh: '已验证', en: 'Validated', bg: 'var(--ok-bg)', tx: 'var(--ok-tx)' },
  refuted: { zh: '已否证', en: 'Refuted', bg: 'var(--danger-bg)', tx: 'var(--danger-tx)' },
};

/** 平铺列表 → 先根深度序（父在前、子跟随缩进）；孤儿节点按原序垫底兜底。 */
function orderWithDepth(nodes: HypothesisNodeRead[]): { node: HypothesisNodeRead; depth: number }[] {
  const byParent = new Map<string | null, HypothesisNodeRead[]>();
  for (const n of nodes) {
    const key = n.parent_id ?? null;
    byParent.set(key, [...(byParent.get(key) ?? []), n]);
  }
  const out: { node: HypothesisNodeRead; depth: number }[] = [];
  const walk = (parent: string | null, depth: number) => {
    for (const n of byParent.get(parent) ?? []) {
      out.push({ node: n, depth });
      walk(n.id, depth + 1);
    }
  };
  walk(null, 0);
  if (out.length < nodes.length) {
    const seen = new Set(out.map((e) => e.node.id));
    for (const n of nodes) if (!seen.has(n.id)) out.push({ node: n, depth: 0 });
  }
  return out;
}

export function DiscoveryTreeCard({ voyage }: { voyage: VoyageRead }) {
  const active = !VOYAGE_TERMINAL.has(voyage.status);
  const { data } = useQuery({
    queryKey: ['hypothesis-tree', voyage.id],
    queryFn: () => api.listHypothesisTree(voyage.id),
    retry: false,
    // 任务还在跑就轮询：树是逐轮长出来的
    refetchInterval: active ? 10_000 : false,
  });
  const entries = useMemo(() => orderWithDepth(data ?? []), [data]);

  return (
    <div className="card card-pad" style={{ marginBottom: 20 }}>
      <div className="row" style={{ marginBottom: 10 }}>
        <span className="section-h">
          <Icon name="compass" size={15} style={{ color: 'var(--accent)' }} />
          {tr('假设树', 'Hypothesis tree')}
        </span>
        {entries.length > 0 && (
          <span className="mono" style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-3)' }}>
            {entries.length} {tr('个节点', 'nodes')}
          </span>
        )}
      </div>
      {entries.length === 0 ? (
        <div style={{ fontSize: 12.5, color: 'var(--text-3)' }}>
          {active
            ? tr('树还没长出来：AI 正在生成根假设…', 'No tree yet — the AI is seeding the root hypothesis…')
            : tr('这次任务没有留下假设树。', 'This run left no hypothesis tree.')}
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {entries.map(({ node, depth }) => {
            const m = NODE_STATUS_META[node.status] ?? OPEN_META;
            return (
              <div key={node.id} className="row gap8" style={{ paddingLeft: depth * 18, alignItems: 'flex-start' }}>
                <span className="pill sm" style={{ background: m.bg, color: m.tx, flexShrink: 0 }}>
                  {tr(m.zh, m.en)}
                </span>
                <span style={{ fontSize: 12.5, minWidth: 0, textDecoration: node.status === 'pruned' ? 'line-through' : undefined, color: node.status === 'pruned' ? 'var(--text-3)' : undefined }}>
                  {node.statement}
                </span>
                {node.score != null && (
                  <span className="mono" style={{ marginLeft: 'auto', fontSize: 10.5, color: 'var(--text-4)', flexShrink: 0 }}>
                    {node.score.toFixed(2)}
                  </span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
