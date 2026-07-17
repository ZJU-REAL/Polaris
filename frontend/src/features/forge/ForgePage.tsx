import { memo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { StatusPill } from '../../components/ui/StatusPill';
import { Segmented } from '../../components/ui/Segmented';
import { Modal } from '../../components/ui/Modal';
import { KnobRange } from '../../components/ui/KnobRange';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { useProject } from '../../app/project';
import { fmtTime } from '../../lib/format';
import { clickable } from '../../lib/a11y';
import { useShell } from '../../app/AppShell';
import {
  api,
  ApiError,
  RESEARCH_TYPES,
  type ForgeState,
  type IdeaDepth,
  type IdeaRead,
  type IdeaSort,
  type IdeaStatus,
} from '../../lib/api';
import { DepthBadge, RESEARCH_TYPE_ZH, ResearchTypeBadge, ScoreRingGroup } from './ideaShared';
import { DeepDiveDrawer } from './DeepDiveDrawer';

/* ============================================================
   /forge — Stage 01 · Idea Forge（M3）
   顶部：当前方向 + forge/state 卡（idea 计数漏斗）+ 运行按钮
   （Modal 成本旋钮 → POST forge）；候选池 CandidateCard 网格，
   点击进入 /ideas/:id 详情。
   ============================================================ */

type StatusFilter = 'all' | IdeaStatus;

type DepthFilter = 'all' | IdeaDepth;

const STATUS_FILTERS: { v: StatusFilter; label: string }[] = [
  { v: 'all', label: '全部' },
  { v: 'candidate', label: '候选' },
  { v: 'under_review', label: '评审中' },
  { v: 'promoted', label: '已晋级' },
  { v: 'rejected', label: '已淘汰' },
];

const SORTS: { v: IdeaSort; label: string }[] = [
  { v: 'elo', label: 'Elo' },
  { v: 'score', label: '评分' },
  { v: '-created_at', label: '最新' },
];

/* ---------------- 收敛漏斗（横向阶段计数条） ---------------- */

const FUNNEL_STAGES: { key: keyof NonNullable<ForgeState['idea_counts']>; zh: string; en: string }[] = [
  { key: 'candidate', zh: '候选', en: 'candidate' },
  { key: 'under_review', zh: '评审中', en: 'under review' },
  { key: 'promoted', zh: '已晋级', en: 'promoted' },
];

function FunnelBar({ state }: { state: ForgeState | undefined }) {
  const counts = state?.idea_counts;
  return (
    <div className="row gap8" style={{ alignItems: 'stretch' }}>
      {FUNNEL_STAGES.map((s, i) => {
        const n = counts?.[s.key];
        return (
          <div key={s.key} className="row gap8" style={{ flex: 1 }}>
            {i > 0 && <Icon name="chevron" size={15} style={{ color: 'var(--text-4)', alignSelf: 'center' }} />}
            <div
              style={{
                flex: 1,
                borderRadius: 10,
                padding: '12px 14px',
                background: i === 2 ? 'var(--ok-bg)' : i === 1 ? 'var(--violet-bg)' : 'var(--accent-soft)',
              }}
            >
              <div
                className="mono"
                style={{
                  fontSize: 20,
                  fontWeight: 700,
                  color: i === 2 ? 'var(--ok-tx)' : i === 1 ? 'var(--violet-tx)' : 'var(--accent-text)',
                }}
              >
                {n ?? '—'}
              </div>
              <div style={{ fontSize: 11.5, fontWeight: 600, marginTop: 2 }}>{s.zh}</div>
              <div className="mono" style={{ fontSize: 9.5, color: 'var(--text-3)' }}>{s.en}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ---------------- 候选卡 ---------------- */

/* memo：onOpen/onDeepen 只捕获稳定引用与 id；onDeepen 的有无（运行中禁用）参与比较 */
const CandidateCard = memo(function CandidateCard({
  idea,
  onOpen,
  onDeepen,
}: {
  idea: IdeaRead;
  onOpen: () => void;
  /** 草案行内「深化为研究方案」入口（进行中任务时不传）。 */
  onDeepen?: () => void;
}) {
  return (
    <div className="card card-pad hoverable" {...clickable(onOpen)} style={{ display: 'flex', flexDirection: 'column' }}>
      <div className="row gap6" style={{ marginBottom: 10 }}>
        <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>{idea.id.slice(0, 8)}</span>
        <DepthBadge depth={idea.depth} />
        <ResearchTypeBadge type={idea.research_type} />
        <span style={{ marginLeft: 'auto' }}>
          <StatusPill status={idea.status} sm />
        </span>
      </div>
      <div style={{ fontSize: 14.5, fontWeight: 650, lineHeight: 1.35, marginBottom: 8 }}>{idea.title}</div>
      <div
        style={{
          fontSize: 12.5,
          color: 'var(--text-2)',
          lineHeight: 1.55,
          marginBottom: 14,
          display: '-webkit-box',
          WebkitLineClamp: 3,
          WebkitBoxOrient: 'vertical',
          overflow: 'hidden',
        }}
      >
        {idea.summary}
      </div>
      <div className="row" style={{ marginTop: 'auto', justifyContent: 'space-between', alignItems: 'flex-end' }}>
        <ScoreRingGroup scores={idea.scores} size={36} />
        <div style={{ textAlign: 'right', flexShrink: 0, marginLeft: 12 }}>
          <div className="mono" style={{ fontSize: 15, fontWeight: 700, color: 'var(--accent-text)' }}>
            {Math.round(idea.elo_rating)}
          </div>
          <div className="mono" style={{ fontSize: 9.5, color: 'var(--text-3)' }}>Elo</div>
        </div>
      </div>
      {idea.depth === 'sketch' && onDeepen && (
        <button
          className="btn btn-soft sm"
          style={{ marginTop: 12, justifyContent: 'center' }}
          onClick={(e) => {
            e.stopPropagation();
            onDeepen();
          }}
        >
          <Icon name="sparkle" size={13} />
          深化为研究方案
        </button>
      )}
    </div>
  );
}, (prev, next) => prev.idea === next.idea && (prev.onDeepen === undefined) === (next.onDeepen === undefined));

/* ---------------- 运行 forge Modal ---------------- */

function RunForgeModal({
  open,
  onClose,
  pid,
}: {
  open: boolean;
  onClose: () => void;
  pid: string;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [numIdeas, setNumIdeas] = useState(8);
  const [dedupThreshold, setDedupThreshold] = useState(0.85);
  const [maxContextPapers, setMaxContextPapers] = useState(20);

  const forgeMutation = useMutation({
    mutationFn: () =>
      api.startForge(pid, {
        num_ideas: numIdeas,
        dedup_threshold: dedupThreshold,
        max_context_papers: maxContextPapers,
      }),
    onSuccess: (v) => {
      toast('想法生成已开始，跳转任务详情…', 'ok');
      void queryClient.invalidateQueries({ queryKey: ['forge-state', pid] });
      onClose();
      navigate(`/voyages/${v.id}`);
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) {
        toast('已有 AI 想法任务在运行，请等待其完成。', 'error');
        void queryClient.invalidateQueries({ queryKey: ['forge-state', pid] });
      } else {
        toast(`启动失败：${e instanceof Error ? e.message : String(e)}`, 'error');
      }
    },
  });

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={
        <>
          <Icon name="bulb" size={16} style={{ color: 'var(--accent)' }} />
          运行 Idea Forge
        </>
      }
      sub="从知识库分析研究空白，生成并筛选候选想法。"
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>取消</button>
          <button className="btn btn-primary" disabled={forgeMutation.isPending} onClick={() => forgeMutation.mutate()}>
            {forgeMutation.isPending ? (
              <>
                <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
                启动中…
              </>
            ) : (
              <>
                <Icon name="play" size={14} />
                开始生成
              </>
            )}
          </button>
        </>
      }
    >
      <KnobRange
        label="生成数量"
        en="num_ideas"
        hint="本次生成的候选想法数（去重前）。"
        value={numIdeas}
        min={3}
        max={20}
        step={1}
        onChange={setNumIdeas}
      />
      <KnobRange
        label="去重阈值"
        en="dedup_threshold"
        hint="语义相似度高于该阈值的想法视为重复。"
        value={dedupThreshold}
        min={0.5}
        max={0.95}
        step={0.05}
        format={(v) => v.toFixed(2)}
        onChange={setDedupThreshold}
      />
      <KnobRange
        label="上下文论文数"
        en="max_context_papers"
        hint="gap 分析时注入的 compiled wiki 页上限（成本旋钮）。"
        value={maxContextPapers}
        min={5}
        max={50}
        step={5}
        onChange={setMaxContextPapers}
      />
    </Modal>
  );
}

/* ---------------- 页面 ---------------- */

export function ForgePage() {
  const navigate = useNavigate();
  const { openGates } = useShell();
  const { projects, isLoading: projectsLoading, currentProject, currentProjectId } = useProject();
  const pid = currentProjectId;

  const [modalOpen, setModalOpen] = useState(false);
  const [deepOpen, setDeepOpen] = useState(false);
  const [deepSeedIdea, setDeepSeedIdea] = useState<{ id: string; title: string } | null>(null);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [depthFilter, setDepthFilter] = useState<DepthFilter>('all');
  const [typeFilter, setTypeFilter] = useState<string>('all');
  const [sort, setSort] = useState<IdeaSort>('elo');

  const stateQuery = useQuery({
    queryKey: ['forge-state', pid],
    queryFn: () => api.getForgeState(pid!),
    enabled: !!pid,
    retry: false,
    refetchInterval: (q) => (q.state.data?.running_voyage_id ? 5_000 : 60_000),
  });
  const state = stateQuery.data;

  // —— 深度生成状态（进行中任务 / 待确认研究目标） ——
  const deepQuery = useQuery({
    queryKey: ['deep-state', pid],
    queryFn: () => api.getDeepIdeaState(pid!),
    enabled: !!pid,
    retry: false,
    refetchInterval: (q) =>
      q.state.data?.running_voyage_id || q.state.data?.pending_gate_id ? 5_000 : 60_000,
  });
  const deep = deepQuery.data;
  const deepRunningId = deep?.running_voyage_id ?? null;
  // forge / 深度生成同项目互斥（后端共用 409）
  const running = !!state?.running_voyage_id || !!deepRunningId;

  const ideasQuery = useQuery({
    queryKey: ['ideas', pid, statusFilter, sort, depthFilter, typeFilter],
    queryFn: () =>
      api.listIdeas(pid!, {
        status: statusFilter === 'all' ? undefined : statusFilter,
        sort,
        depth: depthFilter === 'all' ? undefined : depthFilter,
        research_type: typeFilter === 'all' ? undefined : typeFilter,
      }),
    enabled: !!pid,
    retry: false,
  });
  const ideas = ideasQuery.data ?? [];

  function openDeepDrawer(seedIdea?: { id: string; title: string }) {
    setDeepSeedIdea(seedIdea ?? null);
    setDeepOpen(true);
  }

  // —— 无项目：引导创建 ——
  if (!projectsLoading && projects.length === 0) {
    return (
      <div className="page fadeup">
        <PageHead
          eyebrow="Stage 01 · Idea Forge"
          title="想法生成 Idea Forge"
          sub="从知识库分析研究空白，生成候选想法。"
        />
        <div className="card">
          <EmptyState
            icon="bulb"
            title="还没有研究方向"
            desc="想法生成需要一个研究方向和它的知识库：先创建方向并运行文献初始建库。"
            action={
              <button className="btn btn-primary" onClick={() => navigate('/projects/new')}>
                <Icon name="plus" size={14} />
                新建研究方向 · New direction
              </button>
            }
          />
        </div>
      </div>
    );
  }

  const counts = state?.idea_counts;

  return (
    <div className="page fadeup">
      <PageHead
        eyebrow="Stage 01 · Idea Forge"
        title="想法生成 Idea Forge"
        sub={
          currentProject
            ? `当前方向：${currentProject.name}`
            : projectsLoading
              ? '加载研究方向…'
              : '选择一个研究方向'
        }
        en="gap analysis · candidates · dedup"
        right={
          <div className="row gap8">
            <button className="btn btn-soft" disabled={!pid || running} onClick={() => setModalOpen(true)}>
              <Icon name="play" size={14} />
              运行 Idea Forge
            </button>
            <button className="btn btn-primary" disabled={!pid || running} onClick={() => openDeepDrawer()}>
              {running ? (
                <>
                  <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
                  运行中…
                </>
              ) : (
                <>
                  <Icon name="sparkle" size={14} />
                  深度生成 Deep Dive
                </>
              )}
            </button>
          </div>
        }
      />

      {/* 待确认研究目标提示 */}
      {deep?.pending_gate_id && (
        <div
          className="card card-pad hoverable"
          onClick={() => openGates(deep.pending_gate_id)}
          style={{ marginBottom: 16, borderColor: 'var(--warn)', background: 'var(--warn-bg)' }}
        >
          <div className="row gap10">
            <span className="pill" style={{ background: 'var(--warn-bg)', color: 'var(--warn-tx)' }}>
              <span className="dot pulse" />
              待确认
            </span>
            <span style={{ fontSize: 13.5, fontWeight: 650, color: 'var(--warn-tx)' }}>
              有研究目标待确认 — AI 已完成目标构建，等你确认后继续起草研究方案
            </span>
            <span style={{ marginLeft: 'auto', fontSize: 12, color: 'var(--warn-tx)' }}>去审批</span>
            <Icon name="arrow" size={14} style={{ color: 'var(--warn-tx)' }} />
          </div>
        </div>
      )}

      {/* 深度生成进行中 banner */}
      {deepRunningId && (
        <div
          className="card card-pad hoverable"
          onClick={() => navigate(`/voyages/${deepRunningId}`)}
          style={{ marginBottom: 16, borderColor: 'var(--accent-soft-2)', background: 'var(--accent-soft)' }}
        >
          <div className="row gap10">
            <span className="pill" style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>
              <span className="dot pulse" />
              运行中
            </span>
            <span style={{ fontSize: 13.5, fontWeight: 650 }}>深度生成进行中 — 点击查看目标构建 / 方案起草的实时进度</span>
            <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)', marginLeft: 'auto' }}>
              {deepRunningId.slice(0, 8)}…
            </span>
            <Icon name="arrow" size={14} style={{ color: 'var(--accent-text)' }} />
          </div>
        </div>
      )}

      {/* 进行中任务 banner（深度生成任务另有专属 banner，避免重复） */}
      {state?.running_voyage_id && state.running_voyage_id !== deepRunningId && (
        <div
          className="card card-pad hoverable"
          onClick={() => navigate(`/voyages/${state.running_voyage_id}`)}
          style={{ marginBottom: 16, borderColor: 'var(--accent-soft-2)', background: 'var(--accent-soft)' }}
        >
          <div className="row gap10">
            <span className="pill" style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>
              <span className="dot pulse" />
              运行中
            </span>
            <span style={{ fontSize: 13.5, fontWeight: 650 }}>想法生成任务进行中 — 点击查看实时进度</span>
            <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)', marginLeft: 'auto' }}>
              voyage {state.running_voyage_id.slice(0, 8)}…
            </span>
            <Icon name="arrow" size={14} style={{ color: 'var(--accent-text)' }} />
          </div>
        </div>
      )}

      {/* forge/state 卡：漏斗 + 上次运行 */}
      <div className="card card-pad" style={{ marginBottom: 22 }}>
        <div className="row" style={{ justifyContent: 'space-between', marginBottom: 16 }}>
          <span className="section-h">
            <Icon name="layers" size={15} style={{ color: 'var(--accent)' }} />
            筛选漏斗 <span className="en-label" style={{ fontSize: 11 }}>候选 → 评审中 → 已晋级</span>
          </span>
          <div className="row gap8">
            {counts?.rejected !== undefined && (
              <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-3)' }}>
                已淘汰 <span className="mono" style={{ fontWeight: 700 }}>{counts.rejected}</span>
              </span>
            )}
            {counts?.total !== undefined && (
              <span className="pill sm" style={{ background: 'var(--surface-2)' }}>
                总计 <span className="mono" style={{ fontWeight: 700 }}>{counts.total}</span>
              </span>
            )}
          </div>
        </div>
        {stateQuery.isLoading ? (
          <div className="empty" style={{ padding: 16 }}>加载状态…</div>
        ) : stateQuery.isError ? (
          <div className="empty" style={{ padding: 16 }}>无法加载 forge 状态（后端不可用或接口未就绪）</div>
        ) : (
          <>
            <FunnelBar state={state} />
            <div className="row gap8" style={{ marginTop: 14, fontSize: 11.5, color: 'var(--text-3)' }}>
              <Icon name="clock" size={13} />
              上次运行：
              {state?.last_run?.voyage_id ? (
                <span
                  className="row gap6 hoverable"
                  onClick={() => navigate(`/voyages/${state.last_run?.voyage_id ?? ''}`)}
                  style={{ color: 'var(--accent-text)' }}
                >
                  {state.last_run.status && <StatusPill status={state.last_run.status} sm />}
                  <span className="mono">{fmtTime(state.last_run.finished_at)}</span>
                  <Icon name="chevron" size={12} />
                </span>
              ) : (
                <span>尚未运行过想法生成</span>
              )}
            </div>
          </>
        )}
      </div>

      {/* 候选池 */}
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 14 }}>
        <span className="section-h">
          <Icon name="bulb" size={15} style={{ color: 'var(--accent)' }} />
          候选池 <span className="en-label" style={{ fontSize: 11 }}>{ideas.length} ideas</span>
        </span>
        <div className="row gap10 wrap">
          <select
            className="input"
            style={{ height: 32, fontSize: 12.5, padding: '0 8px' }}
            value={depthFilter}
            title="按草案 / 研究方案过滤"
            onChange={(e) => setDepthFilter(e.target.value as DepthFilter)}
          >
            <option value="all">全部深度</option>
            <option value="sketch">草案</option>
            <option value="proposal">研究方案</option>
          </select>
          <select
            className="input"
            style={{ height: 32, fontSize: 12.5, padding: '0 8px' }}
            value={typeFilter}
            title="按研究类型过滤"
            onChange={(e) => setTypeFilter(e.target.value)}
          >
            <option value="all">全部类型</option>
            {RESEARCH_TYPES.map((t) => (
              <option key={t} value={t}>{RESEARCH_TYPE_ZH[t] ?? t}</option>
            ))}
          </select>
          <Segmented<StatusFilter> options={STATUS_FILTERS} value={statusFilter} onChange={setStatusFilter} />
          <Segmented<IdeaSort> options={SORTS} value={sort} onChange={setSort} />
        </div>
      </div>

      {!pid ? (
        <div className="card">
          <EmptyState compact icon="bulb" title="请先选择研究方向" />
        </div>
      ) : ideasQuery.isLoading ? (
        <div className="card">
          <div className="empty">加载候选池…</div>
        </div>
      ) : ideasQuery.isError ? (
        <div className="card">
          <EmptyState
            compact
            icon="x"
            title="无法加载候选池"
            desc="后端不可用或接口尚未就绪。"
            action={
              <button className="btn btn-soft sm" onClick={() => void ideasQuery.refetch()}>
                重试 retry
              </button>
            }
          />
        </div>
      ) : ideas.length === 0 ? (
        <div className="card">
          <EmptyState
            icon="bulb"
            title="候选池为空"
            desc="运行一次想法生成，从知识库中分析研究空白并生成候选想法。"
            action={
              <button className="btn btn-primary" disabled={running} onClick={() => setModalOpen(true)}>
                <Icon name="play" size={14} />
                运行 Idea Forge
              </button>
            }
          />
        </div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(340px, 1fr))', gap: 16 }}>
          {ideas.map((idea) => (
            <CandidateCard
              key={idea.id}
              idea={idea}
              onOpen={() => navigate(`/ideas/${idea.id}`)}
              onDeepen={running ? undefined : () => openDeepDrawer({ id: idea.id, title: idea.title })}
            />
          ))}
        </div>
      )}

      {pid && <RunForgeModal open={modalOpen} onClose={() => setModalOpen(false)} pid={pid} />}
      {pid && (
        <DeepDiveDrawer
          open={deepOpen}
          onClose={() => setDeepOpen(false)}
          pid={pid}
          initialSeedIdea={deepSeedIdea}
        />
      )}
    </div>
  );
}
