import { memo, useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { StatusPill } from '../../components/ui/StatusPill';
import { EmptyState } from '../../components/ui/EmptyState';
import { Segmented } from '../../components/ui/Segmented';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { toast } from '../../components/ui/Toast';
import { useProject } from '../../app/project';
import { fmtDuration, fmtRelative, fmtTime } from '../../lib/format';
import { api, ApiError, EXPERIMENT_TERMINAL, type ExperimentRead } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { clickable } from '../../lib/a11y';
import { budgetText, expProgress } from './shared';
import { NewExperimentModal } from './NewExperimentModal';

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

/* ============================================================
   /experiment — Stage 03 · Experiment Lab（M4）列表视图。
   实验卡（idea 标题 + status + 服务器 + 耗时 + 进度）+ 新建实验。
   深链 ?new=<idea_id>（Review 页「发起实验」）自动开 Modal。
   ============================================================ */

/* memo：列表页轮询刷新时避免未变卡片重渲染（回调只捕获稳定的 navigate 与 id） */
const ExperimentCard = memo(function ExperimentCard({
  exp,
  mode,
  multiSelect,
  selected,
  onToggleSelect,
  onOpen,
  onTrash,
  onRestore,
  onDelete,
}: {
  exp: ExperimentRead;
  mode: ViewMode;
  multiSelect: boolean;
  selected: boolean;
  onToggleSelect: () => void;
  onOpen: () => void;
  onTrash: () => void;
  onRestore: () => void;
  onDelete: () => void;
}) {
  const isTrash = mode === 'trash';
  const terminal = EXPERIMENT_TERMINAL.has(exp.status);
  const pct = expProgress(exp.status);
  const barColor =
    exp.status === 'failed' ? 'var(--danger)' : exp.status === 'cancelled' ? 'var(--text-4)' : exp.status === 'done' ? 'var(--ok)' : 'var(--accent)';
  // 多选：整卡点击 = 切换选择；否则活动卡点击打开详情，垃圾箱卡不可点。
  const activate = multiSelect ? onToggleSelect : isTrash ? undefined : onOpen;
  return (
    <div
      className={`card card-pad ${activate ? 'hoverable' : ''}`}
      {...(activate ? clickable(activate) : {})}
      style={{ cursor: activate ? 'pointer' : 'default', borderColor: selected ? 'var(--accent)' : undefined }}
    >
      <div className="row gap10" style={{ alignItems: 'flex-start' }}>
        {/* 占位常驻：切换多选时卡片尺寸/位置不变（#132） */}
        <div style={{ paddingTop: 1, visibility: multiSelect ? 'visible' : 'hidden' }}>
          <CheckBox
            checked={selected}
            onToggle={onToggleSelect}
            title={selected ? tr('取消选择', 'Deselect') : tr('选择', 'Select')}
          />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13.5, fontWeight: 650, lineHeight: 1.4, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {exp.idea_title}
          </div>
          <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', marginTop: 3 }}>
            {exp.id.slice(0, 8)} · {tr('创建', 'Created')} {fmtTime(exp.created_at)}
          </div>
        </div>
        <StatusPill status={exp.status} sm />
      </div>
      {isTrash ? (
        <div className="row gap10" style={{ marginTop: 12, alignItems: 'center' }}>
          <span className="mono muted" style={{ fontSize: 11, display: 'inline-flex', alignItems: 'center', gap: 4 }}>
            <Icon name="trash" size={11} />
            {tr('删除于', 'trashed')} {exp.trashed_at ? fmtRelative(exp.trashed_at) : '—'}
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
        <>
          <div className="row gap10" style={{ marginTop: 12, flexWrap: 'wrap' }}>
            <span className="pill sm">
              <Icon name="server" size={11} />
              {exp.server_host ?? tr('未分配', 'Unassigned')}
            </span>
            <span className="pill sm mono" style={{ background: 'var(--surface-3)' }}>{budgetText(exp.budget)}</span>
            <span className="mono muted" style={{ fontSize: 11, marginLeft: 'auto' }}>
              <Icon name="clock" size={11} style={{ display: 'inline-block', verticalAlign: '-1.5px', marginRight: 4 }} />
              {fmtDuration(exp.created_at, terminal ? exp.updated_at : null)}
            </span>
          </div>
          <div className="bar" style={{ marginTop: 12 }}>
            <i style={{ width: `${pct}%`, background: barColor }} />
          </div>
          <div className="row" style={{ marginTop: 10, justifyContent: 'flex-end' }}>
            <button
              className="btn btn-ghost sm"
              title={tr('移入垃圾箱', 'Move to trash')}
              style={{ color: 'var(--text-3)', padding: '0 7px', visibility: multiSelect ? 'hidden' : 'visible' }}
              onClick={(e) => {
                e.stopPropagation();
                onTrash();
              }}
            >
              <Icon name="trash" size={13} />
            </button>
          </div>
        </>
      )}
    </div>
  );
}, (prev, next) =>
  prev.exp === next.exp &&
  prev.mode === next.mode &&
  prev.multiSelect === next.multiSelect &&
  prev.selected === next.selected);

export function ExperimentPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const { projects, isLoading: projectsLoading, currentProject, currentProjectId } = useProject();
  const pid = currentProjectId;

  const newIdeaId = searchParams.get('new');
  const [modalOpen, setModalOpen] = useState(false);
  const [view, setView] = useState<ViewMode>('active');
  const [multiSelect, setMultiSelect] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirm, setConfirm] = useState<
    | null
    | { title: string; message: string; confirmText: string; run: () => void }
  >(null);

  // 深链 ?new=<idea_id> 自动开 Modal
  useEffect(() => {
    if (newIdeaId) setModalOpen(true);
  }, [newIdeaId]);

  function closeModal() {
    setModalOpen(false);
    if (newIdeaId) {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.delete('new');
          return next;
        },
        { replace: true },
      );
    }
  }

  const activeQuery = useQuery({
    queryKey: ['experiments', pid],
    queryFn: () => api.listExperiments(pid!),
    enabled: !!pid,
    retry: false,
    refetchInterval: (q) =>
      (q.state.data ?? []).some((e) => !EXPERIMENT_TERMINAL.has(e.status)) ? 10_000 : false,
  });
  const trashQuery = useQuery({
    queryKey: ['experiments', pid, 'trash'],
    queryFn: () => api.listExperiments(pid!, { trashed: true }),
    enabled: !!pid,
    retry: false,
  });

  const listQuery = view === 'active' ? activeQuery : trashQuery;
  const { isLoading, isError, refetch } = listQuery;
  const experiments = listQuery.data ?? [];
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
  const allSelected = experiments.length > 0 && experiments.every((e) => selected.has(e.id));
  const toggleSelectAll = () =>
    setSelected(allSelected ? new Set() : new Set(experiments.map((e) => e.id)));
  const selectedIds = experiments.filter((e) => selected.has(e.id)).map((e) => e.id);

  const invalidateAll = () => void queryClient.invalidateQueries({ queryKey: ['experiments', pid] });
  const onMutError = (e: unknown) => {
    if (e instanceof ApiError && e.status === 403) {
      toast(tr('只有项目管理者可删除', 'Only project managers can delete'), 'error');
    } else {
      toast(`${tr('操作失败：', 'Action failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
    }
  };

  const trashOne = useMutation({
    mutationFn: (id: string) => api.trashExperiment(id),
    onSuccess: () => {
      invalidateAll();
      toast(tr('已移入垃圾箱', 'Moved to trash'), 'ok');
    },
    onError: onMutError,
  });
  const restoreOne = useMutation({
    mutationFn: (id: string) => api.restoreExperiment(id),
    onSuccess: () => {
      invalidateAll();
      toast(tr('已恢复', 'Restored'), 'ok');
    },
    onError: onMutError,
  });
  const deleteOne = useMutation({
    mutationFn: (id: string) => api.deleteExperimentPermanent(id),
    onSuccess: () => {
      invalidateAll();
      toast(tr('已永久删除', 'Permanently deleted'), 'ok');
    },
    onError: onMutError,
    onSettled: () => setConfirm(null),
  });
  const batchTrash = useMutation({
    mutationFn: (ids: string[]) => api.batchExperiments(pid!, 'trash', ids),
    onSuccess: (r) => {
      invalidateAll();
      clearSelection();
      toast(`${tr('已移入垃圾箱', 'Moved to trash')} · ${r.affected}`, 'ok');
    },
    onError: onMutError,
  });
  const batchRestore = useMutation({
    mutationFn: (ids: string[]) => api.batchExperiments(pid!, 'restore', ids),
    onSuccess: (r) => {
      invalidateAll();
      clearSelection();
      toast(`${tr('已恢复', 'Restored')} · ${r.affected}`, 'ok');
    },
    onError: onMutError,
  });
  const batchDelete = useMutation({
    mutationFn: (ids: string[]) => api.batchExperiments(pid!, 'delete', ids),
    onSuccess: (r) => {
      invalidateAll();
      clearSelection();
      toast(`${tr('已永久删除', 'Permanently deleted')} · ${r.affected}`, 'ok');
    },
    onError: onMutError,
    onSettled: () => setConfirm(null),
  });
  const emptyTrash = useMutation({
    mutationFn: () => api.emptyExperimentTrash(pid!),
    onSuccess: (r) => {
      invalidateAll();
      clearSelection();
      toast(`${tr('垃圾箱已清空', 'Trash emptied')} · ${r.affected}`, 'ok');
    },
    onError: onMutError,
    onSettled: () => setConfirm(null),
  });
  const confirmBusy = deleteOne.isPending || batchDelete.isPending || emptyTrash.isPending;

  if (!projectsLoading && projects.length === 0) {
    return (
      <div className="page fadeup">
        <PageHead
          eyebrow="Stage 03 · Experiment Lab"
          title={tr('实验搭建', 'Experiment Lab')}
          sub={tr('从计划、审批、建环境到运行与报告。', 'From plan, approval and environment setup to runs and report.')}
        />
        <div className="card">
          <EmptyState
            icon="flask"
            title={tr('还没有研究方向', 'No research direction yet')}
            desc={tr('先创建研究方向、生成并晋级想法，再发起实验。', 'Create a direction, generate and promote an idea, then start an experiment.')}
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

  return (
    <div className="page fadeup" style={{ maxWidth: 1180 }}>
      <PageHead
        eyebrow="Stage 03 · Experiment Lab"
        title={tr('实验搭建', 'Experiment Lab')}
        sub={
          currentProject
            ? tr(`当前方向：${currentProject.name}`, `Current direction: ${currentProject.name}`)
            : projectsLoading
              ? tr('加载研究方向…', 'Loading directions…')
              : tr('选择一个研究方向', 'Pick a research direction')
        }
        right={
          <button className="btn btn-primary" disabled={!pid} onClick={() => setModalOpen(true)}>
            <Icon name="plus" size={14} />
            {tr('新建实验', 'New experiment')}
          </button>
        }
      />

      {pid && (
        <div className="row gap10" style={{ marginBottom: 14, alignItems: 'center' }}>
          <Segmented<ViewMode>
            value={view}
            onChange={setView}
            options={[
              { v: 'active', label: tr('实验列表', 'Experiments') },
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
          {/* 全选放工具栏：不再插入额外行导致卡片下移（#132） */}
          {multiSelect && experiments.length > 0 && (
            <>
              <CheckBox checked={allSelected} onToggle={toggleSelectAll} title={tr('全选', 'Select all')} />
              <span className="muted" style={{ fontSize: 12 }}>
                {selected.size > 0
                  ? tr(`已选 ${selected.size} 个`, `${selected.size} selected`)
                  : tr('全选', 'Select all')}
              </span>
            </>
          )}
          {view === 'trash' && trashCount > 0 && (
            <button
              className="btn btn-ghost sm"
              style={{ marginLeft: 'auto', color: 'var(--danger)' }}
              onClick={() =>
                setConfirm({
                  title: tr('清空垃圾箱', 'Empty trash'),
                  message: tr(
                    `将永久删除垃圾箱内全部 ${trashCount} 个实验，不可恢复。确定继续？`,
                    `This permanently deletes all ${trashCount} experiment(s) in the trash. This cannot be undone. Continue?`,
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
        </div>
      )}

      {!pid ? (
        <div className="card">
          <div className="empty" style={{ padding: 60 }}>
            {projectsLoading ? tr('加载研究方向…', 'Loading directions…') : tr('请先选择研究方向', 'Pick a research direction first')}
          </div>
        </div>
      ) : isLoading ? (
        <div className="card">
          <div className="empty" style={{ padding: 60 }}>{view === 'trash' ? tr('加载垃圾箱…', 'Loading trash…') : tr('加载实验列表…', 'Loading experiments…')}</div>
        </div>
      ) : isError ? (
        <div className="card">
          <EmptyState
            compact
            icon="x"
            title={tr('无法加载实验列表', 'Could not load experiments')}
            desc={tr('后端不可用或接口尚未就绪。', 'Backend unavailable or the API is not ready yet.')}
            action={<button className="btn btn-soft sm" onClick={() => void refetch()}>{tr('重试', 'Retry')}</button>}
          />
        </div>
      ) : experiments.length === 0 ? (
        <div className="card">
          {view === 'trash' ? (
            <EmptyState
              compact
              icon="trash"
              title={tr('垃圾箱是空的', 'Trash is empty')}
              desc={tr('删除的实验会先进这里，可恢复或永久删除。', 'Deleted experiments land here first — restore them or delete permanently.')}
            />
          ) : (
            <EmptyState
              icon="flask"
              title={tr('还没有实验', 'No experiments yet')}
              desc={tr('先在想法评审页晋级一个想法，再回到这里发起实验。', 'Promote an idea in Idea Review first, then come back to start an experiment.')}
              action={
                <div className="row gap8">
                  <button className="btn btn-ghost" onClick={() => navigate('/review')}>
                    <Icon name="scale" size={14} />
                    {tr('前往想法评审', 'Go to Idea Review')}
                  </button>
                  <button className="btn btn-primary" onClick={() => setModalOpen(true)}>
                    <Icon name="plus" size={14} />
                    {tr('新建实验', 'New experiment')}
                  </button>
                </div>
              }
            />
          )}
        </div>
      ) : (
        <>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(340px, 1fr))', gap: 14 }}>
            {experiments.map((e) => (
              <ExperimentCard
                key={e.id}
                exp={e}
                mode={view}
                multiSelect={multiSelect}
                selected={selected.has(e.id)}
                onToggleSelect={() => toggleSelect(e.id)}
                onOpen={() => navigate(`/experiment/${e.id}`)}
                onTrash={() => trashOne.mutate(e.id)}
                onRestore={() => restoreOne.mutate(e.id)}
                onDelete={() =>
                  setConfirm({
                    title: tr('永久删除实验', 'Delete experiment permanently'),
                    message: tr(
                      `将永久删除「${e.idea_title}」，不可恢复。确定继续？`,
                      `This permanently deletes "${e.idea_title}". This cannot be undone. Continue?`,
                    ),
                    confirmText: tr('永久删除', 'Delete permanently'),
                    run: () => deleteOne.mutate(e.id),
                  })
                }
              />
            ))}
          </div>
        </>
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
            {tr(`已选 ${selected.size} 个`, `${selected.size} selected`)}
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
                        `将永久删除所选 ${selected.size} 个实验，不可恢复。确定继续？`,
                        `This permanently deletes the ${selected.size} selected experiment(s). This cannot be undone. Continue?`,
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

      {pid && (
        <NewExperimentModal open={modalOpen} onClose={closeModal} pid={pid} initialIdeaId={newIdeaId} />
      )}
    </div>
  );
}
