import { memo, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { StatusPill } from '../../components/ui/StatusPill';
import { Segmented } from '../../components/ui/Segmented';
import { Modal } from '../../components/ui/Modal';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { KnobRange } from '../../components/ui/KnobRange';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { useProject } from '../../app/project';
import { fmtRelative, fmtTime } from '../../lib/format';
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
import { tr } from '../../lib/i18n';
import { DepthBadge, researchTypeLabel, ResearchTypeBadge, ScoreRingGroup } from './ideaShared';
import { DeepDiveDrawer } from './DeepDiveDrawer';

/* ============================================================
   /forge — Stage 01 · Idea Forge（M3）
   顶部：当前方向 + forge/state 卡（idea 计数漏斗）+ 运行按钮
   （Modal 成本旋钮 → POST forge）；候选池 CandidateCard 网格，
   点击进入 /ideas/:id 详情。
   ============================================================ */

type StatusFilter = 'all' | IdeaStatus;

type DepthFilter = 'all' | IdeaDepth;

type ViewMode = 'active' | 'trash';

/** 圆角小勾选框（体系内样式，替代原生 checkbox）。 */
function CheckBox({ checked, onToggle, title }: { checked: boolean; onToggle: () => void; title?: string }) {
  return (
    <button
      type="button"
      title={title}
      onClick={(e) => {
        e.stopPropagation();
        onToggle();
      }}
      style={{
        width: 18,
        height: 18,
        flexShrink: 0,
        borderRadius: 5,
        border: `1.5px solid ${checked ? 'var(--accent)' : 'var(--border-2)'}`,
        background: checked ? 'var(--accent)' : 'transparent',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        cursor: 'pointer',
        padding: 0,
        color: '#fff',
        transition: 'all .12s',
      }}
    >
      {checked && <Icon name="check" size={12} sw={2.4} />}
    </button>
  );
}

/* 文案在渲染处 tr()，避免模块级求值不随语言切换 */
const STATUS_FILTERS: { v: StatusFilter; zh: string; en: string }[] = [
  { v: 'all', zh: '全部', en: 'All' },
  { v: 'candidate', zh: '候选', en: 'Candidate' },
  { v: 'under_review', zh: '评审中', en: 'In review' },
  { v: 'promoted', zh: '已晋级', en: 'Promoted' },
  { v: 'rejected', zh: '已淘汰', en: 'Rejected' },
];

const SORTS: { v: IdeaSort; zh: string; en: string }[] = [
  { v: 'elo', zh: 'Elo', en: 'Elo' },
  { v: 'score', zh: '评分', en: 'Score' },
  { v: '-created_at', zh: '最新', en: 'Newest' },
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
              <div style={{ fontSize: 11.5, fontWeight: 600, marginTop: 2 }}>{tr(s.zh, s.en)}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ---------------- 候选卡 ---------------- */

/* memo：回调只捕获稳定引用与 id；参与比较的是 idea 引用 / 视图模式 / 多选与选中态 / onDeepen 的有无 */
const CandidateCard = memo(function CandidateCard({
  idea,
  mode,
  multiSelect,
  selected,
  onToggleSelect,
  onOpen,
  onDeepen,
  onTrash,
  onRestore,
  onDelete,
}: {
  idea: IdeaRead;
  mode: ViewMode;
  multiSelect: boolean;
  selected: boolean;
  onToggleSelect: () => void;
  onOpen: () => void;
  /** 草案行内「深化为研究方案」入口（进行中任务时不传）。 */
  onDeepen?: () => void;
  onTrash: () => void;
  onRestore: () => void;
  onDelete: () => void;
}) {
  const isTrash = mode === 'trash';
  // 多选：整卡点击 = 切换选择；否则活动卡点击打开详情，垃圾箱卡不可点。
  const activate = multiSelect ? onToggleSelect : isTrash ? undefined : onOpen;
  return (
    <div
      className={`card card-pad ${activate ? 'hoverable' : ''}`}
      {...(activate ? clickable(activate) : {})}
      style={{
        display: 'flex',
        flexDirection: 'column',
        cursor: activate ? 'pointer' : 'default',
        borderColor: selected ? 'var(--accent)' : undefined,
      }}
    >
      <div className="row gap6" style={{ marginBottom: 10, alignItems: 'center' }}>
        {multiSelect && (
          <CheckBox
            checked={selected}
            onToggle={onToggleSelect}
            title={selected ? tr('取消选择', 'Deselect') : tr('选择', 'Select')}
          />
        )}
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
      {isTrash ? (
        <div className="row gap10" style={{ marginTop: 12, alignItems: 'center' }}>
          <span className="mono muted" style={{ fontSize: 11, display: 'inline-flex', alignItems: 'center', gap: 4 }}>
            <Icon name="trash" size={11} />
            {tr('删除于', 'trashed')} {idea.trashed_at ? fmtRelative(idea.trashed_at) : '—'}
          </span>
          <div className="row gap8" style={{ marginLeft: 'auto' }}>
            <button
              className="btn btn-soft sm"
              onClick={(e) => {
                e.stopPropagation();
                onRestore();
              }}
            >
              <Icon name="refresh" size={12} />
              {tr('恢复', 'Restore')}
            </button>
            <button
              className="btn btn-ghost sm"
              onClick={(e) => {
                e.stopPropagation();
                onDelete();
              }}
              style={{ color: 'var(--danger)' }}
            >
              <Icon name="trash" size={12} />
              {tr('永久删除', 'Delete permanently')}
            </button>
          </div>
        </div>
      ) : (
        (idea.depth === 'sketch' && onDeepen) || !multiSelect ? (
          <div className="row gap8" style={{ marginTop: 12, alignItems: 'center' }}>
            {idea.depth === 'sketch' && onDeepen && (
              <button
                className="btn btn-soft sm"
                style={{ justifyContent: 'center' }}
                onClick={(e) => {
                  e.stopPropagation();
                  onDeepen();
                }}
              >
                <Icon name="sparkle" size={13} />
                {tr('深化为研究方案', 'Deepen into proposal')}
              </button>
            )}
            {!multiSelect && (
              <button
                className="btn btn-ghost sm"
                title={tr('移入垃圾箱', 'Move to trash')}
                style={{ marginLeft: 'auto', color: 'var(--text-3)', padding: '0 7px' }}
                onClick={(e) => {
                  e.stopPropagation();
                  onTrash();
                }}
              >
                <Icon name="trash" size={13} />
              </button>
            )}
          </div>
        ) : null
      )}
    </div>
  );
}, (prev, next) =>
  prev.idea === next.idea &&
  prev.mode === next.mode &&
  prev.multiSelect === next.multiSelect &&
  prev.selected === next.selected &&
  (prev.onDeepen === undefined) === (next.onDeepen === undefined));

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
      toast(tr('想法生成已开始，跳转任务详情…', 'Idea generation started — opening the task…'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['forge-state', pid] });
      onClose();
      navigate(`/voyages/${v.id}`);
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) {
        toast(tr('已有 AI 想法任务在运行，请等待其完成。', 'An AI idea task is already running — wait for it to finish.'), 'error');
        void queryClient.invalidateQueries({ queryKey: ['forge-state', pid] });
      } else {
        toast(`${tr('启动失败：', 'Start failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
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
          {tr('运行 Idea Forge', 'Run Idea Forge')}
        </>
      }
      sub={tr('从知识库分析研究空白，生成并筛选候选想法。', 'Analyze research gaps in the knowledge base, then generate and filter candidate ideas.')}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>{tr('取消', 'Cancel')}</button>
          <button className="btn btn-primary" disabled={forgeMutation.isPending} onClick={() => forgeMutation.mutate()}>
            {forgeMutation.isPending ? (
              <>
                <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
                {tr('启动中…', 'Starting…')}
              </>
            ) : (
              <>
                <Icon name="play" size={14} />
                {tr('开始生成', 'Start generating')}
              </>
            )}
          </button>
        </>
      }
    >
      <KnobRange
        label={tr('生成数量', 'Ideas to generate')}
        en="num_ideas"
        hint={tr('本次生成的候选想法数（去重前）。', 'Number of candidate ideas per run (before dedup).')}
        value={numIdeas}
        min={3}
        max={20}
        step={1}
        onChange={setNumIdeas}
      />
      <KnobRange
        label={tr('去重阈值', 'Dedup threshold')}
        en="dedup_threshold"
        hint={tr('语义相似度高于该阈值的想法视为重复。', 'Ideas above this semantic-similarity threshold count as duplicates.')}
        value={dedupThreshold}
        min={0.5}
        max={0.95}
        step={0.05}
        format={(v) => v.toFixed(2)}
        onChange={setDedupThreshold}
      />
      <KnobRange
        label={tr('上下文论文数', 'Context papers')}
        en="max_context_papers"
        hint={tr('gap 分析时注入的 compiled wiki 页上限（成本旋钮）。', 'Max compiled wiki pages fed into gap analysis (a cost knob).')}
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
  const queryClient = useQueryClient();
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
  const [view, setView] = useState<ViewMode>('active');
  const [multiSelect, setMultiSelect] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirm, setConfirm] = useState<
    | null
    | { title: string; message: string; confirmText: string; run: () => void }
  >(null);

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

  const trashQuery = useQuery({
    queryKey: ['ideas', pid, 'trash'],
    queryFn: () => api.listIdeas(pid!, { trashed: true }),
    enabled: !!pid,
    retry: false,
  });

  const listQuery = view === 'active' ? ideasQuery : trashQuery;
  const ideas = listQuery.data ?? [];
  const trashCount = trashQuery.data?.length ?? 0;

  // 切换视图时清空选择（两个列表的 id 集合不同）。
  useEffect(() => {
    setSelected(new Set());
  }, [view]);

  const toggleSelect = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const clearSelection = () => setSelected(new Set());
  const toggleMultiSelect = () =>
    setMultiSelect((on) => {
      if (on) setSelected(new Set());
      return !on;
    });
  const allSelected = ideas.length > 0 && ideas.every((i) => selected.has(i.id));
  const toggleSelectAll = () => setSelected(allSelected ? new Set() : new Set(ideas.map((i) => i.id)));
  const selectedIds = ideas.filter((i) => selected.has(i.id)).map((i) => i.id);

  const invalidateIdeas = () => {
    void queryClient.invalidateQueries({ queryKey: ['ideas', pid] });
    void queryClient.invalidateQueries({ queryKey: ['forge-state', pid] });
  };
  const onMutError = (e: unknown) => {
    if (e instanceof ApiError && e.status === 403) {
      toast(tr('只有项目管理者可删除', 'Only project managers can delete'), 'error');
    } else {
      toast(`${tr('操作失败：', 'Action failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
    }
  };

  const trashOne = useMutation({
    mutationFn: (id: string) => api.trashIdea(id),
    onSuccess: () => {
      invalidateIdeas();
      toast(tr('已移入垃圾箱', 'Moved to trash'), 'ok');
    },
    onError: onMutError,
  });
  const restoreOne = useMutation({
    mutationFn: (id: string) => api.restoreIdea(id),
    onSuccess: () => {
      invalidateIdeas();
      toast(tr('已恢复', 'Restored'), 'ok');
    },
    onError: onMutError,
  });
  const deleteOne = useMutation({
    mutationFn: (id: string) => api.deleteIdeaPermanent(id),
    onSuccess: () => {
      invalidateIdeas();
      toast(tr('已永久删除', 'Permanently deleted'), 'ok');
    },
    onError: onMutError,
    onSettled: () => setConfirm(null),
  });
  const batchTrash = useMutation({
    mutationFn: (ids: string[]) => api.batchIdeas(pid!, 'trash', ids),
    onSuccess: (r) => {
      invalidateIdeas();
      clearSelection();
      toast(`${tr('已移入垃圾箱', 'Moved to trash')} · ${r.affected}`, 'ok');
    },
    onError: onMutError,
  });
  const batchRestore = useMutation({
    mutationFn: (ids: string[]) => api.batchIdeas(pid!, 'restore', ids),
    onSuccess: (r) => {
      invalidateIdeas();
      clearSelection();
      toast(`${tr('已恢复', 'Restored')} · ${r.affected}`, 'ok');
    },
    onError: onMutError,
  });
  const batchDelete = useMutation({
    mutationFn: (ids: string[]) => api.batchIdeas(pid!, 'delete', ids),
    onSuccess: (r) => {
      invalidateIdeas();
      clearSelection();
      toast(`${tr('已永久删除', 'Permanently deleted')} · ${r.affected}`, 'ok');
    },
    onError: onMutError,
    onSettled: () => setConfirm(null),
  });
  const emptyTrash = useMutation({
    mutationFn: () => api.emptyIdeaTrash(pid!),
    onSuccess: (r) => {
      invalidateIdeas();
      clearSelection();
      toast(`${tr('垃圾箱已清空', 'Trash emptied')} · ${r.affected}`, 'ok');
    },
    onError: onMutError,
    onSettled: () => setConfirm(null),
  });
  const confirmBusy = deleteOne.isPending || batchDelete.isPending || emptyTrash.isPending;

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
          title={tr('想法生成', 'Idea Forge')}
          sub={tr('从知识库分析研究空白，生成候选想法。', 'Analyze research gaps in the knowledge base and generate candidate ideas.')}
        />
        <div className="card">
          <EmptyState
            icon="bulb"
            title={tr('还没有研究方向', 'No research direction yet')}
            desc={tr('想法生成需要一个研究方向和它的知识库：先创建方向并运行文献初始建库。', 'Idea Forge needs a direction and its knowledge base: create one and run the initial literature build first.')}
            action={
              <button className="btn btn-primary" onClick={() => navigate('/projects/new')}>
                <Icon name="plus" size={14} />
                {tr('新建研究方向', 'New direction')}
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
        title={tr('想法生成', 'Idea Forge')}
        sub={
          currentProject
            ? tr(`当前方向：${currentProject.name}`, `Current direction: ${currentProject.name}`)
            : projectsLoading
              ? tr('加载研究方向…', 'Loading directions…')
              : tr('选择一个研究方向', 'Pick a research direction')
        }
        right={
          <div className="row gap8">
            <button className="btn btn-soft" disabled={!pid || running} onClick={() => setModalOpen(true)}>
              <Icon name="play" size={14} />
              {tr('运行 Idea Forge', 'Run Idea Forge')}
            </button>
            <button className="btn btn-primary" disabled={!pid || running} onClick={() => openDeepDrawer()}>
              {running ? (
                <>
                  <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
                  {tr('运行中…', 'Running…')}
                </>
              ) : (
                <>
                  <Icon name="sparkle" size={14} />
                  {tr('深度生成', 'Deep Dive')}
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
              {tr('待确认', 'Needs confirmation')}
            </span>
            <span style={{ fontSize: 13.5, fontWeight: 650, color: 'var(--warn-tx)' }}>
              {tr('有研究目标待确认 — AI 已完成目标构建，等你确认后继续起草研究方案', 'A research goal awaits your confirmation — the AI built it and will draft the proposal once you confirm')}
            </span>
            <span style={{ marginLeft: 'auto', fontSize: 12, color: 'var(--warn-tx)' }}>{tr('去审批', 'Open approvals')}</span>
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
              {tr('运行中', 'Running')}
            </span>
            <span style={{ fontSize: 13.5, fontWeight: 650 }}>{tr('深度生成进行中 — 点击查看目标构建 / 方案起草的实时进度', 'Deep Dive in progress — click to watch goal building / proposal drafting live')}</span>
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
              {tr('运行中', 'Running')}
            </span>
            <span style={{ fontSize: 13.5, fontWeight: 650 }}>{tr('想法生成任务进行中 — 点击查看实时进度', 'Idea generation in progress — click to watch live')}</span>
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
            {tr('筛选漏斗', 'Selection funnel')} <span className="en-label" style={{ fontSize: 11 }}>{tr('候选 → 评审中 → 已晋级', 'candidate → in review → promoted')}</span>
          </span>
          <div className="row gap8">
            {counts?.rejected !== undefined && (
              <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-3)' }}>
                {tr('已淘汰', 'Rejected')} <span className="mono" style={{ fontWeight: 700 }}>{counts.rejected}</span>
              </span>
            )}
            {counts?.total !== undefined && (
              <span className="pill sm" style={{ background: 'var(--surface-2)' }}>
                {tr('总计', 'Total')} <span className="mono" style={{ fontWeight: 700 }}>{counts.total}</span>
              </span>
            )}
          </div>
        </div>
        {stateQuery.isLoading ? (
          <div className="empty" style={{ padding: 16 }}>{tr('加载状态…', 'Loading state…')}</div>
        ) : stateQuery.isError ? (
          <div className="empty" style={{ padding: 16 }}>{tr('无法加载 forge 状态（后端不可用或接口未就绪）', 'Could not load forge state (backend unavailable or API not ready)')}</div>
        ) : (
          <>
            <FunnelBar state={state} />
            <div className="row gap8" style={{ marginTop: 14, fontSize: 11.5, color: 'var(--text-3)' }}>
              <Icon name="clock" size={13} />
              {tr('上次运行：', 'Last run: ')}
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
                <span>{tr('尚未运行过想法生成', 'Idea generation has not run yet')}</span>
              )}
            </div>
          </>
        )}
      </div>

      {/* 候选池 */}
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 14, flexWrap: 'wrap', gap: 10 }}>
        <div className="row gap10" style={{ alignItems: 'center', flexWrap: 'wrap' }}>
          <span className="section-h">
            <Icon name="bulb" size={15} style={{ color: 'var(--accent)' }} />
            {tr('候选池', 'Candidate pool')} <span className="en-label" style={{ fontSize: 11 }}>{tr(`${ideas.length} 条`, `${ideas.length} ideas`)}</span>
          </span>
          {pid && (
            <>
              <Segmented<ViewMode>
                value={view}
                onChange={setView}
                options={[
                  { v: 'active', label: tr('想法列表', 'Ideas') },
                  { v: 'trash', label: `${tr('垃圾箱', 'Trash')}${trashCount > 0 ? ` (${trashCount})` : ''}` },
                ]}
              />
              <button
                className={`btn sm ${multiSelect ? 'btn-primary' : 'btn-soft'}`}
                onClick={toggleMultiSelect}
                title={tr('批量选择', 'Multi-select')}
              >
                <Icon name="check" size={13} />
                {tr('多选', 'Multi-select')}
              </button>
              {view === 'trash' && trashCount > 0 && (
                <button
                  className="btn btn-ghost sm"
                  style={{ color: 'var(--danger)' }}
                  onClick={() =>
                    setConfirm({
                      title: tr('清空垃圾箱', 'Empty trash'),
                      message: tr(
                        `将永久删除垃圾箱内全部 ${trashCount} 条想法，不可恢复。确定继续？`,
                        `This permanently deletes all ${trashCount} idea(s) in the trash. This cannot be undone. Continue?`,
                      ),
                      confirmText: tr('清空', 'Empty'),
                      run: () => emptyTrash.mutate(),
                    })
                  }
                >
                  <Icon name="trash" size={13} />
                  {tr('清空垃圾箱', 'Empty trash')}
                </button>
              )}
            </>
          )}
        </div>
        {view === 'active' && (
          <div className="row gap10 wrap">
            <select
              className="input"
              style={{ height: 32, fontSize: 12.5, padding: '0 8px' }}
              value={depthFilter}
              title={tr('按草案 / 研究方案过滤', 'Filter by sketch / proposal')}
              onChange={(e) => setDepthFilter(e.target.value as DepthFilter)}
            >
              <option value="all">{tr('全部深度', 'All depths')}</option>
              <option value="sketch">{tr('草案', 'Sketch')}</option>
              <option value="proposal">{tr('研究方案', 'Proposal')}</option>
            </select>
            <select
              className="input"
              style={{ height: 32, fontSize: 12.5, padding: '0 8px' }}
              value={typeFilter}
              title={tr('按研究类型过滤', 'Filter by research type')}
              onChange={(e) => setTypeFilter(e.target.value)}
            >
              <option value="all">{tr('全部类型', 'All types')}</option>
              {RESEARCH_TYPES.map((t) => (
                <option key={t} value={t}>{researchTypeLabel(t)}</option>
              ))}
            </select>
            <Segmented<StatusFilter>
              options={STATUS_FILTERS.map((f) => ({ v: f.v, label: tr(f.zh, f.en) }))}
              value={statusFilter}
              onChange={setStatusFilter}
            />
            <Segmented<IdeaSort> options={SORTS.map((s) => ({ v: s.v, label: tr(s.zh, s.en) }))} value={sort} onChange={setSort} />
          </div>
        )}
      </div>

      {/* 多选：全选行 */}
      {pid && multiSelect && ideas.length > 0 && (
        <div className="row gap10" style={{ marginBottom: 10, alignItems: 'center' }}>
          <CheckBox checked={allSelected} onToggle={toggleSelectAll} title={tr('全选', 'Select all')} />
          <span className="muted" style={{ fontSize: 12 }}>
            {selected.size > 0
              ? tr(`已选 ${selected.size} 条`, `${selected.size} selected`)
              : tr('全选', 'Select all')}
          </span>
        </div>
      )}

      {!pid ? (
        <div className="card">
          <EmptyState compact icon="bulb" title={tr('请先选择研究方向', 'Pick a research direction first')} />
        </div>
      ) : listQuery.isLoading ? (
        <div className="card">
          <div className="empty">{view === 'trash' ? tr('加载垃圾箱…', 'Loading trash…') : tr('加载候选池…', 'Loading candidates…')}</div>
        </div>
      ) : listQuery.isError ? (
        <div className="card">
          <EmptyState
            compact
            icon="x"
            title={tr('无法加载候选池', 'Could not load candidates')}
            desc={tr('后端不可用或接口尚未就绪。', 'Backend unavailable or the API is not ready yet.')}
            action={
              <button className="btn btn-soft sm" onClick={() => void listQuery.refetch()}>
                {tr('重试', 'Retry')}
              </button>
            }
          />
        </div>
      ) : ideas.length === 0 ? (
        <div className="card">
          {view === 'trash' ? (
            <EmptyState
              compact
              icon="trash"
              title={tr('垃圾箱是空的', 'Trash is empty')}
              desc={tr('删除的想法会先进这里，可恢复或永久删除。', 'Deleted ideas land here first — restore them or delete permanently.')}
            />
          ) : (
            <EmptyState
              icon="bulb"
              title={tr('候选池为空', 'The candidate pool is empty')}
              desc={tr('运行一次想法生成，从知识库中分析研究空白并生成候选想法。', 'Run idea generation to analyze research gaps in the knowledge base and produce candidates.')}
              action={
                <button className="btn btn-primary" disabled={running} onClick={() => setModalOpen(true)}>
                  <Icon name="play" size={14} />
                  {tr('运行 Idea Forge', 'Run Idea Forge')}
                </button>
              }
            />
          )}
        </div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(340px, 1fr))', gap: 16 }}>
          {ideas.map((idea) => (
            <CandidateCard
              key={idea.id}
              idea={idea}
              mode={view}
              multiSelect={multiSelect}
              selected={selected.has(idea.id)}
              onToggleSelect={() => toggleSelect(idea.id)}
              onOpen={() => navigate(`/ideas/${idea.id}`)}
              onDeepen={view === 'trash' || running ? undefined : () => openDeepDrawer({ id: idea.id, title: idea.title })}
              onTrash={() => trashOne.mutate(idea.id)}
              onRestore={() => restoreOne.mutate(idea.id)}
              onDelete={() =>
                setConfirm({
                  title: tr('永久删除想法', 'Delete idea permanently'),
                  message: tr(
                    `将永久删除「${idea.title}」，不可恢复。确定继续？`,
                    `This permanently deletes "${idea.title}". This cannot be undone. Continue?`,
                  ),
                  confirmText: tr('永久删除', 'Delete permanently'),
                  run: () => deleteOne.mutate(idea.id),
                })
              }
            />
          ))}
        </div>
      )}

      {/* 批量操作栏（多选模式下有选中时浮出底部） */}
      {pid && multiSelect && selected.size > 0 && (
        <div
          className="card card-pad"
          style={{
            position: 'sticky',
            bottom: 16,
            marginTop: 16,
            boxShadow: 'var(--shadow-pop)',
            display: 'flex',
            alignItems: 'center',
            gap: 12,
          }}
        >
          <span style={{ fontSize: 12.5, fontWeight: 600 }}>
            {tr(`已选 ${selected.size} 条`, `${selected.size} selected`)}
          </span>
          <div className="row gap8" style={{ marginLeft: 'auto' }}>
            {view === 'active' ? (
              <button
                className="btn btn-danger sm"
                disabled={batchTrash.isPending}
                onClick={() => batchTrash.mutate(selectedIds)}
              >
                <Icon name="trash" size={13} />
                {tr('批量删除', 'Delete selected')}
              </button>
            ) : (
              <>
                <button
                  className="btn btn-soft sm"
                  disabled={batchRestore.isPending}
                  onClick={() => batchRestore.mutate(selectedIds)}
                >
                  <Icon name="refresh" size={13} />
                  {tr('批量恢复', 'Restore selected')}
                </button>
                <button
                  className="btn btn-danger sm"
                  disabled={batchDelete.isPending}
                  onClick={() =>
                    setConfirm({
                      title: tr('永久删除所选', 'Delete selected permanently'),
                      message: tr(
                        `将永久删除所选 ${selected.size} 条想法，不可恢复。确定继续？`,
                        `This permanently deletes the ${selected.size} selected idea(s). This cannot be undone. Continue?`,
                      ),
                      confirmText: tr('永久删除', 'Delete permanently'),
                      run: () => batchDelete.mutate(selectedIds),
                    })
                  }
                >
                  <Icon name="trash" size={13} />
                  {tr('批量永久删除', 'Delete permanently')}
                </button>
              </>
            )}
            <button className="btn btn-ghost sm" onClick={clearSelection}>
              {tr('取消选择', 'Clear')}
            </button>
          </div>
        </div>
      )}

      <ConfirmModal
        open={!!confirm}
        onClose={() => setConfirm(null)}
        title={confirm?.title ?? ''}
        message={confirm?.message ?? ''}
        confirmText={confirm?.confirmText}
        danger
        busy={confirmBusy}
        onConfirm={() => confirm?.run()}
      />

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
