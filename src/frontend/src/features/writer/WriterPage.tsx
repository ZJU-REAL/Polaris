import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { DotsMenu, type DotsMenuItem } from '../../components/ui/DotsMenu';
import { EmptyState, ErrorState, LoadingState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { topicPath, useProject } from '../../app/project';
import { errorText } from '../../lib/errors';
import { fmtRelative } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { api, type ManuscriptRead } from '../../lib/api';
import { useDirections } from '../deepresearch/directions';
import { useResearchRun } from '../deepresearch/hooks';
import { saveBlob } from '../wiki/shared';
import { NewManuscriptModal } from './NewManuscriptModal';
import '../deepresearch/deepresearch.css';
import '../voyages/task.css';
import '../forge/ideas.css';
import '../experiment/experiment.css';

/* ============================================================
   课题的「写作」页（/t/:id/writer）：稿件列表。

   和「想法」「实验」同一种读法：一行一篇稿件（标题、状态、模板、什么时候改过），
   一个主按钮「新建稿件」，置顶 / 导出 / 删除在每行的「更多」里，批量管理和回收站
   在页面的「更多」里。

   这台电脑没有装 LaTeX 时在这里直说：稿件可以照常起草和编辑，只是不能编译成 PDF。
   ============================================================ */

type View = 'active' | 'trash';

function CheckBox({ checked, onToggle, label }: { checked: boolean; onToggle: () => void; label: string }) {
  return (
    <button type="button" role="checkbox" aria-checked={checked} aria-label={label} title={label} className={'ideas-check' + (checked ? ' is-on' : '')} onClick={onToggle}>
      {checked && <Icon name="check" size={12} sw={2.4} />}
    </button>
  );
}

/** AI 正在起草（引擎写 writing，旧数据里也有 drafting）。 */
function isDrafting(status: string): boolean {
  return status === 'writing' || status === 'drafting';
}

/** 稿件状态，一句话。 */
export function manuscriptSentence(status: string): string {
  switch (status) {
    case 'draft':
      return tr('草稿', 'Draft');
    case 'drafting':
    case 'writing':
      return tr('AI 正在起草', 'The AI is drafting');
    case 'compiled':
      return tr('已编译出 PDF', 'Compiled to PDF');
    case 'under_review':
      return tr('评审中', 'In review');
    case 'approved':
      return tr('评审通过', 'Passed review');
    case 'submitted':
      return tr('已投稿', 'Submitted');
    default:
      return status;
  }
}

/** 引擎所在的电脑上有没有 LaTeX。只有研究的读数里带这一项；一次研究都没有时不知道（undefined）。 */
export function useLatexAvailable(pid: string | null): boolean | undefined {
  const { byProject, directions } = useDirections();
  const run = byProject(pid)?.run ?? directions.find((d) => d.run)?.run ?? null;
  const runQuery = useResearchRun(run?.id ?? '');
  return runQuery.data?.capabilities?.latex;
}

export function WriterPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { currentProjectId: pid } = useProject();
  const [modalOpen, setModalOpen] = useState(false);
  const [view, setView] = useState<View>('active');
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirm, setConfirm] = useState<null | { title: string; message: string; confirmText: string; run: () => void }>(null);
  const latex = useLatexAvailable(pid);

  const activeQuery = useQuery({
    queryKey: ['manuscripts', pid],
    queryFn: () => api.listManuscripts(pid!),
    enabled: !!pid,
    retry: false,
    // 状态流转靠通知实时刷新；起草中留 30 秒慢轮询兜底
    refetchInterval: (q) => ((q.state.data ?? []).some((m) => isDrafting(m.status)) ? 30_000 : false),
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

  const manuscripts = useMemo(
    () => [...(activeQuery.data ?? [])].sort((a, b) => Number(!!b.pinned_at) - Number(!!a.pinned_at) || b.updated_at.localeCompare(a.updated_at)),
    [activeQuery.data],
  );
  const trash = trashQuery.data ?? [];
  const list = view === 'trash' ? trash : manuscripts;

  useEffect(() => setSelected(new Set()), [view]);
  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const selectedIds = list.filter((m) => selected.has(m.id)).map((m) => m.id);

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ['manuscripts', pid] });
  const onErr = (e: unknown) => toast(errorText(e), 'error');
  const countText = (n: number) => tr(`${n} 篇稿件`, n === 1 ? '1 manuscript' : `${n} manuscripts`);

  const trashOne = useMutation({
    mutationFn: (id: string) => api.trashManuscript(id),
    onSuccess: () => {
      invalidate();
      toast(tr('已移入回收站', 'Moved to trash'), 'ok');
    },
    onError: onErr,
  });
  const restoreOne = useMutation({
    mutationFn: (id: string) => api.restoreManuscript(id),
    onSuccess: () => {
      invalidate();
      toast(tr('已恢复', 'Restored'), 'ok');
    },
    onError: onErr,
  });
  const deleteOne = useMutation({
    mutationFn: (id: string) => api.deleteManuscriptPermanent(id),
    onSuccess: () => {
      invalidate();
      toast(tr('已永久删除', 'Deleted permanently'), 'ok');
    },
    onError: onErr,
    onSettled: () => setConfirm(null),
  });
  const pinOne = useMutation({
    mutationFn: ({ id, pinned }: { id: string; pinned: boolean }) => api.patchManuscript(id, { pinned }),
    onSuccess: (_r, v) => {
      invalidate();
      toast(v.pinned ? tr('已置顶', 'Pinned') : tr('已取消置顶', 'Unpinned'), 'ok');
    },
    onError: onErr,
  });
  const exportOne = useMutation({
    mutationFn: (m: ManuscriptRead) => api.exportManuscriptArxiv(m.id).then((r) => ({ ...r, m })),
    onSuccess: ({ blob, notes, m }) => {
      const safe = (m.title || 'manuscript').replace(/[/\\?%*:|"<>]/g, '_');
      saveBlob(blob, `${safe}-arxiv.tar.gz`);
      if (notes.length > 0) toast(`${tr('导出提示：', 'Export notes: ')}${notes.join('；')}`, 'info');
      else toast(tr('已导出 arXiv 投稿包', 'Exported the arXiv package'), 'ok');
    },
    onError: onErr,
  });
  const batch = useMutation({
    mutationFn: (v: { action: 'trash' | 'restore' | 'delete'; ids: string[] }) => api.batchManuscripts(pid!, v.action, v.ids),
    onSuccess: (r, v) => {
      invalidate();
      setSelected(new Set());
      toast(
        v.action === 'trash'
          ? tr(`已将 ${countText(r.affected)}移入回收站`, `Moved ${countText(r.affected)} to trash`)
          : v.action === 'restore'
            ? tr(`已恢复 ${countText(r.affected)}`, `Restored ${countText(r.affected)}`)
            : tr(`已永久删除 ${countText(r.affected)}`, `Deleted ${countText(r.affected)} permanently`),
        'ok',
      );
    },
    onError: onErr,
    onSettled: () => setConfirm(null),
  });
  const emptyTrash = useMutation({
    mutationFn: () => api.emptyManuscriptTrash(pid!),
    onSuccess: () => {
      invalidate();
      toast(tr('已清空回收站', 'Emptied trash'), 'ok');
    },
    onError: onErr,
    onSettled: () => setConfirm(null),
  });

  if (!pid) return null;

  const confirmModal = (
    <ConfirmModal
      open={!!confirm}
      onClose={() => setConfirm(null)}
      title={confirm?.title ?? ''}
      message={confirm?.message ?? ''}
      confirmText={confirm?.confirmText}
      danger
      busy={deleteOne.isPending || batch.isPending || emptyTrash.isPending}
      onConfirm={() => confirm?.run()}
    />
  );

  const latexNote =
    latex === false ? (
      <div className="ideas-compare is-warn" data-testid="latex-missing">
        {tr(
          '这台电脑没有安装 LaTeX：稿件可以照常起草和编辑，但不能编译成 PDF，也就不能做稿件评审。',
          'LaTeX isn’t installed on this computer: you can draft and edit as usual, but manuscripts can’t be compiled to PDF, so they can’t be reviewed either.',
        )}
      </div>
    ) : null;

  /* —— 回收站 —— */
  if (view === 'trash') {
    return (
      <div className="page fadeup">
        <div className="exp-body">
          <button type="button" className="task-back ideas-backlink" onClick={() => { setView('active'); setSelecting(false); }}>
            ← {tr('写作', 'Writing')}
          </button>
          <div className="ideas-head">
            <div className="ideas-head-text">
              <h2 className="ideas-lead">{tr('回收站', 'Trash')}</h2>
              <span className="ideas-note">{trash.length > 0 ? countText(trash.length) : ''}</span>
            </div>
            {trash.length > 0 && (
              <div className="ideas-head-actions">
                <button type="button" className="btn btn-plain sm" onClick={() => { setSelecting((s) => !s); setSelected(new Set()); }}>
                  {selecting ? tr('完成', 'Done') : tr('批量管理', 'Select several')}
                </button>
                <button
                  type="button"
                  className="btn btn-danger-soft sm"
                  onClick={() =>
                    setConfirm({
                      title: tr('清空回收站？', 'Empty the trash?'),
                      message: tr(`其中的 ${countText(trash.length)}会被永久删除，无法恢复。`, `Its ${countText(trash.length)} will be deleted permanently. This can’t be undone.`),
                      confirmText: tr('清空', 'Empty trash'),
                      run: () => emptyTrash.mutate(),
                    })
                  }
                >
                  {tr('清空回收站', 'Empty trash')}
                </button>
              </div>
            )}
          </div>
          {trashQuery.isLoading ? (
            <LoadingState />
          ) : trash.length === 0 ? (
            <EmptyState compact icon="trash" title={tr('回收站是空的', 'Trash is empty')} />
          ) : (
            <div className="dr-rows exp-rows">
              {trash.map((m) => (
                <div key={m.id} className="exp-row" data-ms={m.id}>
                  {selecting && <CheckBox checked={selected.has(m.id)} onToggle={() => toggle(m.id)} label={tr('选择', 'Select')} />}
                  <div className="exp-row-main">
                    <span className="dr-row-name">{m.title}</span>
                    <span className="exp-row-state">
                      {tr('删除于', 'Deleted')} {m.trashed_at ? fmtRelative(m.trashed_at) : '—'}
                    </span>
                  </div>
                  {!selecting && (
                    <>
                      <button type="button" className="btn btn-ghost sm" disabled={restoreOne.isPending} onClick={() => restoreOne.mutate(m.id)}>
                        {tr('恢复', 'Restore')}
                      </button>
                      <button
                        type="button"
                        className="btn btn-danger-soft sm"
                        style={{ marginRight: 8 }}
                        onClick={() =>
                          setConfirm({
                            title: tr(`永久删除「${m.title}」？`, `Delete “${m.title}” permanently?`),
                            message: tr('删除后无法恢复。', 'This can’t be undone.'),
                            confirmText: tr('永久删除', 'Delete'),
                            run: () => deleteOne.mutate(m.id),
                          })
                        }
                      >
                        {tr('永久删除', 'Delete')}
                      </button>
                    </>
                  )}
                </div>
              ))}
            </div>
          )}
          {selecting && selected.size > 0 && (
            <div className="ideas-batch">
              <span className="ideas-batch-count">{tr(`已选 ${selected.size} 篇`, `${selected.size} selected`)}</span>
              <button type="button" className="btn btn-ghost sm" disabled={batch.isPending} onClick={() => batch.mutate({ action: 'restore', ids: selectedIds })}>
                {tr('恢复', 'Restore')}
              </button>
              <button
                type="button"
                className="btn btn-danger-soft sm"
                onClick={() =>
                  setConfirm({
                    title: tr(`永久删除 ${countText(selected.size)}？`, `Delete ${countText(selected.size)} permanently?`),
                    message: tr('删除后无法恢复。', 'This can’t be undone.'),
                    confirmText: tr('永久删除', 'Delete'),
                    run: () => batch.mutate({ action: 'delete', ids: selectedIds }),
                  })
                }
              >
                {tr('永久删除', 'Delete permanently')}
              </button>
            </div>
          )}
        </div>
        {confirmModal}
      </div>
    );
  }

  /* —— 列表 —— */
  const pageMenu: DotsMenuItem[] = [
    { key: 'select', label: selecting ? tr('退出批量管理', 'Stop selecting') : tr('批量管理', 'Select several'), icon: 'check', onSelect: () => { setSelecting((s) => !s); setSelected(new Set()); } },
    { key: 'review', label: tr('稿件评审', 'Manuscript review'), icon: 'scale', onSelect: () => navigate(topicPath(pid, 'paper-review')) },
    { key: 'trash', label: trash.length > 0 ? tr(`回收站（${trash.length}）`, `Trash (${trash.length})`) : tr('回收站', 'Trash'), icon: 'trash', separated: true, onSelect: () => { setView('trash'); setSelecting(false); } },
  ];
  const isEmpty = !activeQuery.isLoading && !activeQuery.isError && manuscripts.length === 0;

  return (
    <div className="page fadeup">
      <div className="exp-body">
        {activeQuery.isLoading ? (
          <LoadingState rows={3} />
        ) : activeQuery.isError ? (
          <ErrorState title={tr('无法加载稿件，请重试', 'Couldn’t load manuscripts. Try again.')} detail={errorText(activeQuery.error)} onRetry={() => void activeQuery.refetch()} />
        ) : isEmpty ? (
          <div className="exp-needs" data-testid="writer-empty">
            <h2 className="exp-needs-title">{tr('还没有稿件', 'No manuscripts yet')}</h2>
            <p className="exp-needs-lead">
              {tr(
                '稿件是一份 LaTeX 论文。新建时可以关联一个想法和它做完的实验，AI 会据此起草初稿；之后你在编辑器里改，也可以让 AI 改某一段。不关联也行，从空白模板开始写。',
                'A manuscript is a LaTeX paper. Link an idea and its finished experiment when you create one and the AI drafts from them; then you edit it, or ask the AI to rework a passage. You can also start from an empty template.',
              )}
            </p>
            {latexNote}
            <div className="exp-needs-action">
              <button type="button" className="btn btn-primary" onClick={() => setModalOpen(true)}>
                {tr('新建稿件', 'New manuscript')}
              </button>
              {trash.length > 0 && (
                <button type="button" className="btn btn-plain sm" onClick={() => setView('trash')}>
                  {tr(`回收站（${trash.length}）`, `Trash (${trash.length})`)}
                </button>
              )}
            </div>
          </div>
        ) : (
          <>
            <div className="ideas-head">
              <div className="ideas-head-text">
                <h2 className="ideas-lead">{countText(manuscripts.length)}</h2>
              </div>
              <div className="ideas-head-actions">
                <button type="button" className="btn btn-primary sm" onClick={() => setModalOpen(true)}>
                  {tr('新建稿件', 'New manuscript')}
                </button>
                <DotsMenu items={pageMenu} />
              </div>
            </div>
            {latexNote}

            {selecting && (
              <div className="ideas-selectbar">
                <CheckBox
                  checked={manuscripts.every((m) => selected.has(m.id))}
                  onToggle={() => setSelected(manuscripts.every((m) => selected.has(m.id)) ? new Set() : new Set(manuscripts.map((m) => m.id)))}
                  label={tr('全选', 'Select all')}
                />
                <span>{selected.size > 0 ? tr(`已选 ${selected.size} 篇`, `${selected.size} selected`) : tr('全选', 'Select all')}</span>
                <button type="button" className="btn btn-danger-soft sm" disabled={selected.size === 0 || batch.isPending} onClick={() => batch.mutate({ action: 'trash', ids: selectedIds })}>
                  {tr('移入回收站', 'Move to trash')}
                </button>
                <button type="button" className="btn btn-plain sm" onClick={() => { setSelecting(false); setSelected(new Set()); }}>
                  {tr('完成', 'Done')}
                </button>
              </div>
            )}

            <div className="dr-rows exp-rows" data-testid="ms-list">
              {manuscripts.map((m) => {
                const drafting = isDrafting(m.status);
                return (
                  <div key={m.id} className="exp-row" data-ms={m.id} data-status={m.status}>
                    {selecting && <CheckBox checked={selected.has(m.id)} onToggle={() => toggle(m.id)} label={tr('选择', 'Select')} />}
                    <Link className="exp-row-main" to={`/writer/${m.id}`}>
                      <span className="dr-row-name" title={m.title}>
                        {m.title}
                      </span>
                      <span className="exp-row-state">
                        <span className={'task-dot' + (drafting ? ' is-active' : '')} aria-hidden="true" />
                        <span>
                          {[m.pinned_at ? tr('已置顶', 'Pinned') : '', manuscriptSentence(m.status), templateName(m.template)].filter(Boolean).join(' · ')}
                        </span>
                        <span className="exp-row-meta">
                          {tr('更新于', 'Updated')} {fmtRelative(m.updated_at)}
                        </span>
                      </span>
                    </Link>
                    {!selecting && (
                      <DotsMenu
                        plain
                        label={tr('更多操作', 'More actions')}
                        items={[
                          { key: 'pin', label: m.pinned_at ? tr('取消置顶', 'Unpin') : tr('置顶', 'Pin'), icon: 'pin', onSelect: () => pinOne.mutate({ id: m.id, pinned: !m.pinned_at }) },
                          { key: 'export', label: tr('导出 arXiv 投稿包', 'Export arXiv package'), icon: 'download', disabled: exportOne.isPending, onSelect: () => exportOne.mutate(m) },
                          { key: 'trash', label: tr('移入回收站', 'Move to trash'), icon: 'trash', danger: true, separated: true, onSelect: () => trashOne.mutate(m.id) },
                        ]}
                      />
                    )}
                  </div>
                );
              })}
            </div>
          </>
        )}
      </div>

      {confirmModal}
      <NewManuscriptModal open={modalOpen} onClose={() => setModalOpen(false)} pid={pid} latex={latex} />
    </div>
  );
}
