import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { StatusPill } from '../../components/ui/StatusPill';
import { EmptyState } from '../../components/ui/EmptyState';
import { Segmented } from '../../components/ui/Segmented';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { toast } from '../../components/ui/Toast';
import { useProject } from '../../app/project';
import { fmtRelative } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { api, ApiError, type ManuscriptRead } from '../../lib/api';
import { NewManuscriptModal } from './NewManuscriptModal';
import { CollaboratorsModal } from './CollaboratorsModal';
import { saveBlob } from '../wiki/shared';

/* ============================================================
   /writer — Stage 04 · Paper Writer（M5-B）列表视图。
   项目内论文卡（标题 / 模板 / 状态 / 更新时间）+ 新建论文草稿。
   支持多选批量删除、垃圾箱（软删除 / 恢复 / 永久删除 / 清空）。
   ============================================================ */

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

/** 卡片底部的图标动作按钮（幽灵样式，纯图标 + title）。 */
function CardAction({
  icon,
  title,
  onClick,
  active,
  danger,
  disabled,
}: {
  icon: 'pin' | 'share' | 'download' | 'trash';
  title: string;
  onClick: () => void;
  active?: boolean;
  danger?: boolean;
  disabled?: boolean;
}) {
  const color = active ? 'var(--accent)' : danger ? 'var(--danger)' : 'var(--text-3)';
  return (
    <button
      className="btn btn-ghost sm"
      title={title}
      disabled={disabled}
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      style={{ color, padding: '0 7px' }}
    >
      <Icon name={icon} size={13} />
    </button>
  );
}

function ManuscriptCard({
  m,
  templateName,
  mode,
  multiSelect,
  selected,
  exporting,
  onToggleSelect,
  onOpen,
  onPin,
  onShare,
  onExport,
  onTrash,
  onRestore,
  onDelete,
}: {
  m: ManuscriptRead;
  templateName: string;
  mode: ViewMode;
  multiSelect: boolean;
  selected: boolean;
  exporting: boolean;
  onToggleSelect: () => void;
  onOpen: () => void;
  onPin: () => void;
  onShare: () => void;
  onExport: () => void;
  onTrash: () => void;
  onRestore: () => void;
  onDelete: () => void;
}) {
  const isTrash = mode === 'trash';
  const isPinned = !!m.pinned_at;
  return (
    <div
      className={`card card-pad ${multiSelect || !isTrash ? 'hoverable' : ''}`}
      onClick={multiSelect ? onToggleSelect : isTrash ? undefined : onOpen}
      style={{
        cursor: multiSelect || !isTrash ? 'pointer' : 'default',
        borderColor: selected ? 'var(--accent)' : undefined,
      }}
    >
      <div className="row gap10" style={{ alignItems: 'flex-start' }}>
        {/* 占位常驻：切换多选时卡片尺寸/位置不变（#132） */}
        <div
          style={{ paddingTop: 1, visibility: multiSelect ? 'visible' : 'hidden' }}
          onClick={(e) => e.stopPropagation()}
        >
          <CheckBox
            checked={selected}
            onToggle={onToggleSelect}
            title={selected ? tr('取消选择', 'Deselect') : tr('选择', 'Select')}
          />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="row gap6" style={{ minWidth: 0 }}>
            {isPinned && !isTrash && (
              <Icon name="pin" size={13} style={{ color: 'var(--accent)', flexShrink: 0 }} />
            )}
            <div
              style={{
                fontSize: 13.5,
                fontWeight: 650,
                lineHeight: 1.4,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                minWidth: 0,
              }}
              title={m.title}
            >
              {m.title}
            </div>
          </div>
          <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', marginTop: 3 }}>
            {m.id.slice(0, 8)} · {tr('更新', 'updated')} {fmtRelative(m.updated_at)}
          </div>
        </div>
        <StatusPill status={m.status} sm />
      </div>

      {isTrash ? (
        <div className="row gap10" style={{ marginTop: 12, alignItems: 'center' }}>
          <span className="mono muted" style={{ fontSize: 11, display: 'inline-flex', alignItems: 'center', gap: 4 }}>
            <Icon name="trash" size={11} />
            {tr('删除于', 'trashed')} {m.trashed_at ? fmtRelative(m.trashed_at) : '—'}
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
          <span className="pill sm">
            <Icon name="file" size={11} />
            {templateName}
          </span>
          {m.status === 'writing' && (
            <span className="pill sm" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
              <Icon name="sparkle" size={11} />
              {tr('AI 正在写', 'AI writing')}
            </span>
          )}
          <div className="row gap2" style={{ marginLeft: 'auto' }}>
            <CardAction
              icon="pin"
              active={isPinned}
              title={isPinned ? tr('取消置顶', 'Unpin') : tr('置顶', 'Pin to top')}
              onClick={onPin}
            />
            <CardAction icon="share" title={tr('分享', 'Share')} onClick={onShare} />
            <CardAction
              icon="download"
              title={tr('导出 arXiv 投稿包', 'Export arXiv package')}
              onClick={onExport}
              disabled={exporting}
            />
            <CardAction icon="trash" title={tr('移入垃圾箱', 'Move to trash')} onClick={onTrash} />
          </div>
        </div>
      )}
    </div>
  );
}

export function WriterPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { isLoading: projectsLoading, currentProject, currentProjectId } = useProject();
  const pid = currentProjectId;
  const [modalOpen, setModalOpen] = useState(false);
  const [view, setView] = useState<ViewMode>('active');
  const [multiSelect, setMultiSelect] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [shareId, setShareId] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<
    | null
    | { title: string; message: string; confirmText: string; run: () => void }
  >(null);

  const activeQuery = useQuery({
    queryKey: ['manuscripts', pid],
    queryFn: () => api.listManuscripts(pid!),
    enabled: !!pid,
    retry: false,
    // 状态流转靠 WS manuscript.status 实时 invalidate（AppShell）；起草中留 30s 慢轮询兜底
    refetchInterval: (q) => ((q.state.data ?? []).some((m) => m.status === 'writing') ? 30_000 : false),
  });

  const trashQuery = useQuery({
    queryKey: ['manuscripts', pid, 'trash'],
    queryFn: () => api.listManuscripts(pid!, { trashed: true }),
    enabled: !!pid,
    retry: false,
  });

  const templatesQuery = useQuery({
    queryKey: ['manuscript-templates', pid],
    queryFn: () => api.listManuscriptTemplates(pid ?? undefined),
    retry: false,
    staleTime: 5 * 60_000,
  });
  const templateName = (key: string) => templatesQuery.data?.find((t) => t.id === key)?.name ?? key;

  const activeQ = view === 'active' ? activeQuery : trashQuery;
  const manuscripts = activeQ.data ?? [];
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
      if (on) setSelected(new Set()); // 关闭多选时清空已选
      return !on;
    });
  const allSelected = manuscripts.length > 0 && manuscripts.every((m) => selected.has(m.id));
  const toggleSelectAll = () =>
    setSelected(allSelected ? new Set() : new Set(manuscripts.map((m) => m.id)));
  const selectedIds = manuscripts.filter((m) => selected.has(m.id)).map((m) => m.id);

  const invalidateAll = () => void queryClient.invalidateQueries({ queryKey: ['manuscripts', pid] });
  const onMutError = (e: unknown) => {
    if (e instanceof ApiError && e.status === 403) {
      toast(tr('只有项目管理者可删除', 'Only project managers can delete'), 'error');
    } else {
      toast(`${tr('操作失败：', 'Action failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
    }
  };

  const trashOne = useMutation({
    mutationFn: (id: string) => api.trashManuscript(id),
    onSuccess: () => {
      invalidateAll();
      toast(tr('已移入垃圾箱', 'Moved to trash'), 'ok');
    },
    onError: onMutError,
  });
  const restoreOne = useMutation({
    mutationFn: (id: string) => api.restoreManuscript(id),
    onSuccess: () => {
      invalidateAll();
      toast(tr('已恢复', 'Restored'), 'ok');
    },
    onError: onMutError,
  });
  const deleteOne = useMutation({
    mutationFn: (id: string) => api.deleteManuscriptPermanent(id),
    onSuccess: () => {
      invalidateAll();
      toast(tr('已永久删除', 'Permanently deleted'), 'ok');
    },
    onError: onMutError,
    onSettled: () => setConfirm(null),
  });
  const pinOne = useMutation({
    mutationFn: ({ id, pinned }: { id: string; pinned: boolean }) => api.patchManuscript(id, { pinned }),
    onSuccess: (_r, v) => {
      invalidateAll();
      toast(v.pinned ? tr('已置顶', 'Pinned to top') : tr('已取消置顶', 'Unpinned'), 'ok');
    },
    onError: onMutError,
  });
  const exportOne = useMutation({
    mutationFn: (m: ManuscriptRead) => api.exportManuscriptArxiv(m.id).then((r) => ({ ...r, m })),
    onSuccess: ({ blob, notes, m }) => {
      const safe = (m.title || 'manuscript').replace(/[/\\?%*:|"<>]/g, '_');
      saveBlob(blob, `${safe}-arxiv.tar.gz`);
      if (notes.length > 0) toast(`${tr('导出提示：', 'Export notes: ')}${notes.join('；')}`, 'info');
      else toast(tr('已导出 arXiv 投稿包', 'arXiv package exported'), 'ok');
    },
    onError: (e) => toast(`${tr('导出失败：', 'Export failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  const batchTrash = useMutation({
    mutationFn: (ids: string[]) => api.batchManuscripts(pid!, 'trash', ids),
    onSuccess: (r) => {
      invalidateAll();
      clearSelection();
      toast(`${tr('已移入垃圾箱', 'Moved to trash')} · ${r.affected}`, 'ok');
    },
    onError: onMutError,
  });
  const batchRestore = useMutation({
    mutationFn: (ids: string[]) => api.batchManuscripts(pid!, 'restore', ids),
    onSuccess: (r) => {
      invalidateAll();
      clearSelection();
      toast(`${tr('已恢复', 'Restored')} · ${r.affected}`, 'ok');
    },
    onError: onMutError,
  });
  const batchDelete = useMutation({
    mutationFn: (ids: string[]) => api.batchManuscripts(pid!, 'delete', ids),
    onSuccess: (r) => {
      invalidateAll();
      clearSelection();
      toast(`${tr('已永久删除', 'Permanently deleted')} · ${r.affected}`, 'ok');
    },
    onError: onMutError,
    onSettled: () => setConfirm(null),
  });
  const emptyTrash = useMutation({
    mutationFn: () => api.emptyManuscriptTrash(pid!),
    onSuccess: (r) => {
      invalidateAll();
      clearSelection();
      toast(`${tr('垃圾箱已清空', 'Trash emptied')} · ${r.affected}`, 'ok');
    },
    onError: onMutError,
    onSettled: () => setConfirm(null),
  });

  const confirmBusy = deleteOne.isPending || batchDelete.isPending || emptyTrash.isPending;

  return (
    <div className="page fadeup" style={{ maxWidth: 1180 }}>
      <PageHead
        eyebrow="Stage 04 · Paper Writer"
        title={tr('论文撰写', 'Paper Writer')}
        sub={
          currentProject
            ? `${tr('当前课题：', 'Current topic: ')}${currentProject.name}`
            : projectsLoading
              ? tr('加载课题…', 'Loading topics…')
              : tr('选择一个课题', 'Pick a topic')
        }
        right={
          <button className="btn btn-primary" disabled={!pid} onClick={() => setModalOpen(true)}>
            <Icon name="plus" size={14} />
            {tr('新建论文草稿', 'New manuscript')}
          </button>
        }
      />

      {pid && (
        <div className="row gap10" style={{ marginBottom: 14, alignItems: 'center' }}>
          <Segmented
            value={view}
            onChange={(v) => setView(v)}
            options={[
              { v: 'active', label: tr('论文列表', 'Manuscripts') },
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
          {multiSelect && manuscripts.length > 0 && (
            <>
              <CheckBox checked={allSelected} onToggle={toggleSelectAll} title={tr('全选', 'Select all')} />
              <span className="muted" style={{ fontSize: 12 }}>
                {selected.size > 0
                  ? tr(`已选 ${selected.size} 篇`, `${selected.size} selected`)
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
                    `将永久删除垃圾箱内全部 ${trashCount} 篇论文草稿，不可恢复。确定继续？`,
                    `This permanently deletes all ${trashCount} manuscript(s) in the trash. This cannot be undone. Continue?`,
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
            {projectsLoading ? tr('加载课题…', 'Loading topics…') : tr('请先选择课题', 'Pick a topic first')}
          </div>
        </div>
      ) : activeQ.isLoading ? (
        <div className="card">
          <div className="empty" style={{ padding: 60 }}>{tr('加载论文列表…', 'Loading manuscripts…')}</div>
        </div>
      ) : activeQ.isError ? (
        <div className="card">
          <EmptyState
            compact
            icon="x"
            title={tr('无法加载论文列表', 'Failed to load manuscripts')}
            desc={tr('后端不可用或接口尚未就绪。', 'Backend unavailable or API not ready.')}
            action={<button className="btn btn-soft sm" onClick={() => void activeQ.refetch()}>{tr('重试', 'Retry')}</button>}
          />
        </div>
      ) : manuscripts.length === 0 ? (
        <div className="card">
          {view === 'trash' ? (
            <EmptyState
              compact
              icon="trash"
              title={tr('垃圾箱是空的', 'Trash is empty')}
              desc={tr('删除的论文草稿会先进这里，可恢复或永久删除。', 'Deleted manuscripts land here first — restore them or delete permanently.')}
            />
          ) : (
            <EmptyState
              icon="pen"
              title={tr('还没有论文草稿', 'No manuscripts yet')}
              desc={tr(
                '新建一篇：选会议模板、关联已完成的实验，平台会自动组装事实包，AI 就能按真实结果起草。',
                'Create one: pick a venue template and link a finished experiment — the platform assembles a fact pack so AI can draft from real results.',
              )}
              action={
                <button className="btn btn-primary" onClick={() => setModalOpen(true)}>
                  <Icon name="plus" size={14} />
                  {tr('新建论文草稿', 'New manuscript')}
                </button>
              }
            />
          )}
        </div>
      ) : (
        <>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(340px, 1fr))', gap: 14 }}>
            {manuscripts.map((m) => (
              <ManuscriptCard
                key={m.id}
                m={m}
                templateName={templateName(m.template)}
                mode={view}
                multiSelect={multiSelect}
                selected={selected.has(m.id)}
                exporting={exportOne.isPending && exportOne.variables?.id === m.id}
                onToggleSelect={() => toggleSelect(m.id)}
                onOpen={() => navigate(`/writer/${m.id}`)}
                onPin={() => pinOne.mutate({ id: m.id, pinned: !m.pinned_at })}
                onShare={() => setShareId(m.id)}
                onExport={() => exportOne.mutate(m)}
                onTrash={() => trashOne.mutate(m.id)}
                onRestore={() => restoreOne.mutate(m.id)}
                onDelete={() =>
                  setConfirm({
                    title: tr('永久删除论文草稿', 'Delete manuscript permanently'),
                    message: tr(
                      `将永久删除「${m.title}」，不可恢复。确定继续？`,
                      `This permanently deletes "${m.title}". This cannot be undone. Continue?`,
                    ),
                    confirmText: tr('永久删除', 'Delete permanently'),
                    run: () => deleteOne.mutate(m.id),
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
            {tr(`已选 ${selected.size} 篇`, `${selected.size} selected`)}
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
                        `将永久删除所选 ${selected.size} 篇论文草稿，不可恢复。确定继续？`,
                        `This permanently deletes the ${selected.size} selected manuscript(s). This cannot be undone. Continue?`,
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

      {pid && <NewManuscriptModal open={modalOpen} onClose={() => setModalOpen(false)} pid={pid} />}

      <CollaboratorsModal open={!!shareId} manuscriptId={shareId ?? ''} onClose={() => setShareId(null)} />

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
    </div>
  );
}
