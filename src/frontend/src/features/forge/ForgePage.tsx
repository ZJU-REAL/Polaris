import { memo, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { StatusPill } from '../../components/ui/StatusPill';
import { Segmented } from '../../components/ui/Segmented';
import { Modal } from '../../components/ui/Modal';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { ConfigRow, RangeControl } from '../voyages/shared/ConfigRow';
import { StatusDot, TaskStatus } from '../voyages/shared/StatusDot';
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
  { key: 'candidate', zh: '候选', en: 'Candidates' },
  { key: 'under_review', zh: '评审中', en: 'In review' },
  { key: 'promoted', zh: '已晋级', en: 'Promoted' },
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
                  fontWeight: 600,
                  color: i === 2 ? 'var(--ok-tx)' : i === 1 ? 'var(--violet-tx)' : 'var(--accent-text)',
                }}
              >
                {n ?? '—'}
              </div>
              <div style={{ fontSize: 12, fontWeight: 600, marginTop: 2 }}>{tr(s.zh, s.en)}</div>
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
  // 多选：整卡点击 = 切换选择；否则活动卡点击打开详情，回收站卡不可点。
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
        {/* 占位常驻：切换多选时卡片尺寸/位置不变（#132） */}
        <span style={{ display: 'inline-flex', flexShrink: 0, visibility: multiSelect ? 'visible' : 'hidden' }}>
          <CheckBox
            checked={selected}
            onToggle={onToggleSelect}
            title={selected ? tr('取消选择', 'Deselect') : tr('选择', 'Select')}
          />
        </span>
        <DepthBadge depth={idea.depth} />
        <ResearchTypeBadge type={idea.research_type} />
        <span style={{ marginLeft: 'auto' }}>
          <StatusPill status={idea.status} sm />
        </span>
      </div>
      <div style={{ fontSize: 15, fontWeight: 600, lineHeight: 1.35, marginBottom: 8 }}>{idea.title}</div>
      <div
        style={{
          fontSize: 13,
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
          <div className="mono" style={{ fontSize: 15, fontWeight: 600, color: 'var(--accent-text)' }}>
            {Math.round(idea.elo_rating)}
          </div>
          <div className="mono" style={{ fontSize: 10, color: 'var(--text-3)' }}>Elo</div>
        </div>
      </div>
      {isTrash ? (
        <div className="row gap10" style={{ marginTop: 12, alignItems: 'center' }}>
          <span className="mono muted" style={{ fontSize: 11, display: 'inline-flex', alignItems: 'center', gap: 4 }}>
            <Icon name="trash" size={11} />
            {tr('删除于', 'Trashed')} {idea.trashed_at ? fmtRelative(idea.trashed_at) : '—'}
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
              {tr('展开为研究方案', 'Develop into proposal')}
            </button>
          )}
          <button
            className="btn btn-ghost sm"
            title={tr('移入回收站', 'Move to trash')}
            style={{ marginLeft: 'auto', color: 'var(--text-3)', padding: '0 7px', visibility: multiSelect ? 'hidden' : 'visible' }}
            onClick={(e) => {
              e.stopPropagation();
              onTrash();
            }}
          >
            <Icon name="trash" size={13} />
          </button>
        </div>
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
      toast(tr('已开始生成想法', 'Generating ideas'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['forge-state', pid] });
      onClose();
      navigate(`/voyages/${v.id}`);
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) {
        toast(tr('已有想法任务在运行，请等它完成', 'An idea task is already running. Wait for it to finish.'), 'error');
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
          {tr('生成想法', 'Generate ideas')}
        </>
      }
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
                {tr('开始生成', 'Generate')}
              </>
            )}
          </button>
        </>
      }
    >
      <div className="settings-list">
        <ConfigRow label={tr('想法数量', 'Number of ideas')} hint={tr('去重前的数量', 'Before duplicates are removed')}>
          <RangeControl ariaLabel={tr('想法数量', 'Number of ideas')} value={numIdeas} min={3} max={20} step={1} onChange={setNumIdeas} />
        </ConfigRow>
        <ConfigRow label={tr('去重阈值', 'Duplicate threshold')} hint={tr('相似度高于此值视为重复', 'Ideas more similar than this are duplicates')}>
          <RangeControl
            ariaLabel={tr('去重阈值', 'Duplicate threshold')}
            value={dedupThreshold}
            min={0.5}
            max={0.95}
            step={0.05}
            format={(v) => v.toFixed(2)}
            onChange={setDedupThreshold}
          />
        </ConfigRow>
        <ConfigRow label={tr('参考论文数', 'Papers to draw on')} hint={tr('越多越全面，用量也越大', 'More papers, broader ideas, more usage')}>
          <RangeControl
            ariaLabel={tr('参考论文数', 'Papers to draw on')}
            value={maxContextPapers}
            min={5}
            max={50}
            step={5}
            onChange={setMaxContextPapers}
          />
        </ConfigRow>
      </div>
    </Modal>
  );
}

/* ---------------- 页面 ---------------- */

export function ForgePage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { openGates } = useShell();
  const { isLoading: projectsLoading, currentProjectId } = useProject();
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
      toast(tr('没有删除权限', 'You can’t delete this'), 'error');
    } else {
      toast(`${tr('操作失败：', 'Action failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
    }
  };

  const trashOne = useMutation({
    mutationFn: (id: string) => api.trashIdea(id),
    onSuccess: () => {
      invalidateIdeas();
      toast(tr('已移入回收站', 'Moved to trash'), 'ok');
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
      toast(tr(`已将 ${r.affected} 条移入回收站`, `Moved ${r.affected} to trash`), 'ok');
    },
    onError: onMutError,
  });
  const batchRestore = useMutation({
    mutationFn: (ids: string[]) => api.batchIdeas(pid!, 'restore', ids),
    onSuccess: (r) => {
      invalidateIdeas();
      clearSelection();
      toast(tr(`已恢复 ${r.affected} 条`, `Restored ${r.affected}`), 'ok');
    },
    onError: onMutError,
  });
  const batchDelete = useMutation({
    mutationFn: (ids: string[]) => api.batchIdeas(pid!, 'delete', ids),
    onSuccess: (r) => {
      invalidateIdeas();
      clearSelection();
      toast(tr(`已永久删除 ${r.affected} 条`, `Deleted ${r.affected} permanently`), 'ok');
    },
    onError: onMutError,
    onSettled: () => setConfirm(null),
  });
  const emptyTrash = useMutation({
    mutationFn: () => api.emptyIdeaTrash(pid!),
    onSuccess: (r) => {
      invalidateIdeas();
      clearSelection();
      toast(tr(`已清空回收站，删除 ${r.affected} 条`, `Trash emptied: ${r.affected} deleted`), 'ok');
    },
    onError: onMutError,
    onSettled: () => setConfirm(null),
  });
  const confirmBusy = deleteOne.isPending || batchDelete.isPending || emptyTrash.isPending;

  function openDeepDrawer(seedIdea?: { id: string; title: string }) {
    setDeepSeedIdea(seedIdea ?? null);
    setDeepOpen(true);
  }

  const counts = state?.idea_counts;

  return (
    <div className="page fadeup">
      <PageHead
        eyebrow="Stage 01 · Ideas"
        title={tr('想法生成', 'Ideas')}
        sub={
          pid
            ? undefined
            : projectsLoading
              ? tr('加载课题…', 'Loading topics…')
              : tr('选择一个课题', 'Pick a topic')
        }
      />

      {/* —— 顶部一行：视图标签在左，运行入口在右 —— */}
      <div className="row page-tabs" style={{ marginBottom: 14 }}>
        {pid && (
          <Segmented<ViewMode>
            value={view}
            onChange={setView}
            options={[
              { v: 'active', label: tr('想法列表', 'Ideas') },
              { v: 'trash', label: `${tr('回收站', 'Trash')}${trashCount > 0 ? ` (${trashCount})` : ''}` },
            ]}
          />
        )}
        <div style={{ marginLeft: 'auto' }}>
          <div className="row gap8">
            <button className="btn btn-soft sm" disabled={!pid || running} onClick={() => setModalOpen(true)}>
              <Icon name="play" size={13} />
              {tr('生成想法', 'Generate ideas')}
            </button>
            <button className="btn btn-primary sm" disabled={!pid || running} onClick={() => openDeepDrawer()}>
              {running ? (
                <>
                  <Icon name="refresh" size={13} style={{ animation: 'spin 1s linear infinite' }} />
                  {tr('运行中…', 'Running…')}
                </>
              ) : (
                <>
                  <Icon name="sparkle" size={13} />
                  {tr('深度生成', 'Deep dive')}
                </>
              )}
            </button>
          </div>
        </div>
      </div>

      {/* 待确认研究目标提示 */}
      {deep?.pending_gate_id && (
        <div
          className="card card-pad hoverable"
          onClick={() => openGates(deep.pending_gate_id)}
          style={{ marginBottom: 16, borderColor: 'var(--warn)', background: 'var(--warn-bg)' }}
        >
          <div className="row gap10">
            <StatusDot tone="warn" label={tr('待审批', 'Needs approval')} />
            <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--warn-tx)', minWidth: 0 }}>
              {tr('研究目标已生成，批准后开始起草研究方案', 'Approve the research goal to start drafting the proposal')}
            </span>
            <span style={{ marginLeft: 'auto', fontSize: 12, color: 'var(--warn-tx)', flexShrink: 0 }}>{tr('查看', 'Review')}</span>
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
            <span className="dot pulse" style={{ background: 'var(--accent)', flexShrink: 0 }} />
            <span style={{ fontSize: 13, fontWeight: 600, minWidth: 0 }}>{tr('正在深度生成', 'Deep dive in progress')}</span>
            <span style={{ fontSize: 12, color: 'var(--accent-text)', marginLeft: 'auto', flexShrink: 0 }}>{tr('查看进度', 'View progress')}</span>
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
            <span className="dot pulse" style={{ background: 'var(--accent)', flexShrink: 0 }} />
            <span style={{ fontSize: 13, fontWeight: 600, minWidth: 0 }}>{tr('正在生成想法', 'Generating ideas')}</span>
            <span style={{ fontSize: 12, color: 'var(--accent-text)', marginLeft: 'auto', flexShrink: 0 }}>{tr('查看进度', 'View progress')}</span>
            <Icon name="arrow" size={14} style={{ color: 'var(--accent-text)' }} />
          </div>
        </div>
      )}

      {/* forge/state 卡：漏斗 + 上次运行 */}
      <div className="card card-pad" style={{ marginBottom: 22 }}>
        <div className="row" style={{ justifyContent: 'space-between', marginBottom: 16 }}>
          <span className="section-h">
            {tr('想法概况', 'Overview')}
          </span>
          <div className="row gap8">
            {counts?.rejected !== undefined && (
              <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-3)' }}>
                {tr('已淘汰', 'Rejected')} <span className="mono" style={{ fontWeight: 600 }}>{counts.rejected}</span>
              </span>
            )}
            {counts?.total !== undefined && (
              <span className="pill sm" style={{ background: 'var(--surface-2)' }}>
                {tr('总计', 'Total')} <span className="mono" style={{ fontWeight: 600 }}>{counts.total}</span>
              </span>
            )}
          </div>
        </div>
        {stateQuery.isLoading ? (
          <div className="empty" style={{ padding: 16 }}>{tr('加载中…', 'Loading…')}</div>
        ) : stateQuery.isError ? (
          <div className="empty" style={{ padding: 16 }}>{tr('无法加载想法概况，请确认本机引擎在运行', 'Couldn’t load the overview. Make sure the local engine is running.')}</div>
        ) : (
          <>
            <FunnelBar state={state} />
            <div className="row gap8" style={{ marginTop: 14, fontSize: 12, color: 'var(--text-3)' }}>
              <Icon name="clock" size={13} />
              {tr('上次运行：', 'Last run: ')}
              {state?.last_run?.voyage_id ? (
                <span
                  className="row gap6 hoverable"
                  onClick={() => navigate(`/voyages/${state.last_run?.voyage_id ?? ''}`)}
                  style={{ color: 'var(--accent-text)' }}
                >
                  {state.last_run.status && <TaskStatus status={state.last_run.status} />}
                  <span className="mono">{fmtTime(state.last_run.finished_at)}</span>
                  <Icon name="chevron" size={12} />
                </span>
              ) : (
                <span>{tr('还没有生成过想法', 'No ideas generated yet')}</span>
              )}
            </div>
          </>
        )}
      </div>

      {/* 候选池 */}
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 14, flexWrap: 'wrap', gap: 10 }}>
        <div className="row gap10" style={{ alignItems: 'center', flexWrap: 'wrap' }}>
          <span className="section-h">
            {tr('想法', 'Ideas')} <span className="en-label" style={{ fontSize: 11 }}>{tr(`${ideas.length} 条`, ideas.length === 1 ? '1 idea' : `${ideas.length} ideas`)}</span>
          </span>
          {pid && (
            <>
              <button
                className={`btn sm ${multiSelect ? 'btn-primary' : 'btn-soft'}`}
                onClick={toggleMultiSelect}
                title={tr('批量选择', 'Select several')}
              >
                <Icon name="check" size={13} />
                {tr('多选', 'Select')}
              </button>
              {/* 全选放工具栏：不再插入额外行导致卡片下移（#132） */}
              {multiSelect && ideas.length > 0 && (
                <>
                  <CheckBox checked={allSelected} onToggle={toggleSelectAll} title={tr('全选', 'Select all')} />
                  <span className="muted" style={{ fontSize: 12 }}>
                    {selected.size > 0
                      ? tr(`已选 ${selected.size} 条`, `${selected.size} selected`)
                      : tr('全选', 'Select all')}
                  </span>
                </>
              )}
              {view === 'trash' && trashCount > 0 && (
                <button
                  className="btn btn-ghost sm"
                  style={{ color: 'var(--danger)' }}
                  onClick={() =>
                    setConfirm({
                      title: tr('清空回收站？', 'Empty the trash?'),
                      message: tr(
                        `其中 ${trashCount} 条想法会被永久删除，无法恢复。`,
                        trashCount === 1
                          ? 'The idea in it will be deleted permanently. This can’t be undone.'
                          : `Its ${trashCount} ideas will be deleted permanently. This can’t be undone.`,
                      ),
                      confirmText: tr('清空', 'Empty trash'),
                      run: () => emptyTrash.mutate(),
                    })
                  }
                >
                  <Icon name="trash" size={13} />
                  {tr('清空回收站', 'Empty trash')}
                </button>
              )}
            </>
          )}
        </div>
        {view === 'active' && (
          <div className="row gap10 wrap">
            <select
              className="input"
              style={{ height: 32, fontSize: 13, padding: '0 8px' }}
              value={depthFilter}
              title={tr('按草案或研究方案筛选', 'Filter by sketch or proposal')}
              onChange={(e) => setDepthFilter(e.target.value as DepthFilter)}
            >
              <option value="all">{tr('草案和方案', 'Sketches & proposals')}</option>
              <option value="sketch">{tr('草案', 'Sketch')}</option>
              <option value="proposal">{tr('研究方案', 'Proposal')}</option>
            </select>
            <select
              className="input"
              style={{ height: 32, fontSize: 13, padding: '0 8px' }}
              value={typeFilter}
              title={tr('按研究类型筛选', 'Filter by research type')}
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

      {!pid ? (
        <div className="card">
          <EmptyState compact icon="bulb" title={tr('请先选择课题', 'Pick a topic first')} />
        </div>
      ) : listQuery.isLoading ? (
        <div className="card">
          <div className="empty">{tr('加载中…', 'Loading…')}</div>
        </div>
      ) : listQuery.isError ? (
        <div className="card">
          <EmptyState
            compact
            icon="x"
            title={tr('无法加载想法', 'Couldn’t load ideas')}
            desc={tr('请确认本机引擎在运行，然后重试。', 'Make sure the local engine is running, then retry.')}
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
              title={tr('回收站是空的', 'Trash is empty')}
            />
          ) : (
            <EmptyState
              icon="bulb"
              title={tr('还没有想法', 'No ideas yet')}
              action={
                <button className="btn btn-soft" disabled={running} onClick={() => setModalOpen(true)}>
                  <Icon name="play" size={14} />
                  {tr('生成想法', 'Generate ideas')}
                </button>
              }
            />
          )}
        </div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(min(340px, 100%), 1fr))', gap: 16 }}>
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
                  title: tr(`永久删除「${idea.title}」？`, `Delete “${idea.title}” permanently?`),
                  message: tr('删除后无法恢复。', 'This can’t be undone.'),
                  confirmText: tr('永久删除', 'Delete'),
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
          <span style={{ fontSize: 13, fontWeight: 600 }}>
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
                {tr('移入回收站', 'Move to trash')}
              </button>
            ) : (
              <>
                <button
                  className="btn btn-soft sm"
                  disabled={batchRestore.isPending}
                  onClick={() => batchRestore.mutate(selectedIds)}
                >
                  <Icon name="refresh" size={13} />
                  {tr('恢复', 'Restore')}
                </button>
                <button
                  className="btn btn-danger sm"
                  disabled={batchDelete.isPending}
                  onClick={() =>
                    setConfirm({
                      title: tr(
                        `永久删除 ${selected.size} 条想法？`,
                        selected.size === 1 ? 'Delete 1 idea permanently?' : `Delete ${selected.size} ideas permanently?`,
                      ),
                      message: tr('删除后无法恢复。', 'This can’t be undone.'),
                      confirmText: tr('永久删除', 'Delete'),
                      run: () => batchDelete.mutate(selectedIds),
                    })
                  }
                >
                  <Icon name="trash" size={13} />
                  {tr('永久删除', 'Delete permanently')}
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
