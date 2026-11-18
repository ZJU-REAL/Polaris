import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { DotsMenu, type DotsMenuItem } from '../../components/ui/DotsMenu';
import { EmptyState, ErrorState, LoadingState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { topicPath, useProject } from '../../app/project';
import { fmtRelative } from '../../lib/format';
import { errorText } from '../../lib/errors';
import { api, EXPERIMENT_TERMINAL, type ExperimentRead } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { useNow } from '../deepresearch/hooks';
import { elapsedText } from '../deepresearch/phase';
import { experimentSentence, experimentTone, needsYou } from './experimentView';
import { NewExperimentModal } from './NewExperimentModal';
import '../deepresearch/deepresearch.css';
import '../voyages/task.css';
import '../forge/ideas.css';
import './experiment.css';

/* ============================================================
   课题的「实验」页（/t/:id/experiment）。

   一张列表：每个实验一行——为哪个想法做的、现在在做什么或在等你什么、
   在哪台机器上、用了多久。等你处理的排最前。一个主按钮「新建实验」。

   还没有实验时不只说「没有」：实验需要两样东西（一个想法、一台能用 SSH 连的远程
   机器），各一行写明有没有；缺哪样，主按钮就带你去补哪样。

   深链 ?new=<想法 id> 直接打开新建对话框（从想法页点「开始实验」过来）。
   ============================================================ */

type View = 'active' | 'trash';

function CheckBox({ checked, onToggle, label }: { checked: boolean; onToggle: () => void; label: string }) {
  return (
    <button type="button" role="checkbox" aria-checked={checked} aria-label={label} title={label} className={'ideas-check' + (checked ? ' is-on' : '')} onClick={onToggle}>
      {checked && <Icon name="check" size={12} sw={2.4} />}
    </button>
  );
}

/** 等你处理的在最前，其次是还在做的，再是已结束的；同组内新的在前。 */
function rank(e: ExperimentRead): number {
  if (needsYou(e)) return 0;
  return EXPERIMENT_TERMINAL.has(e.status) ? 2 : 1;
}

export function ExperimentPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const { currentProjectId: pid } = useProject();

  const newIdeaId = searchParams.get('new');
  const [modalOpen, setModalOpen] = useState(false);
  const [view, setView] = useState<View>('active');
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirm, setConfirm] = useState<null | { title: string; message: string; confirmText: string; run: () => void }>(null);

  // 深链 ?new=<idea_id> 自动打开对话框
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
    refetchInterval: (q) => ((q.state.data ?? []).some((e) => !EXPERIMENT_TERMINAL.has(e.status)) ? 10_000 : false),
  });
  const trashQuery = useQuery({
    queryKey: ['experiments', pid, 'trash'],
    queryFn: () => api.listExperiments(pid!, { trashed: true }),
    enabled: !!pid,
    retry: false,
  });
  const experiments = useMemo(
    () => [...(activeQuery.data ?? [])].sort((a, b) => rank(a) - rank(b) || b.created_at.localeCompare(a.created_at)),
    [activeQuery.data],
  );
  const trash = trashQuery.data ?? [];
  const isEmpty = !activeQuery.isLoading && !activeQuery.isError && experiments.length === 0;

  // 空状态要说清楚还缺什么：想法、远程机器
  const ideasQuery = useQuery({
    queryKey: ['ideas', pid, 'all'],
    queryFn: () => api.listIdeas(pid!),
    enabled: !!pid && isEmpty,
    retry: false,
  });
  const credsQuery = useQuery({
    queryKey: ['ssh-credentials'],
    queryFn: () => api.listSshCredentials(),
    enabled: isEmpty,
    retry: false,
  });

  const anyActive = experiments.some((e) => !EXPERIMENT_TERMINAL.has(e.status));
  const now = useNow(30_000, anyActive);

  useEffect(() => setSelected(new Set()), [view]);
  const list = view === 'trash' ? trash : experiments;
  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const selectedIds = list.filter((e) => selected.has(e.id)).map((e) => e.id);

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ['experiments', pid] });
  const onErr = (e: unknown) => toast(errorText(e), 'error');
  const trashOne = useMutation({
    mutationFn: (id: string) => api.trashExperiment(id),
    onSuccess: () => {
      invalidate();
      toast(tr('已移入回收站', 'Moved to trash'), 'ok');
    },
    onError: onErr,
  });
  const restoreOne = useMutation({
    mutationFn: (id: string) => api.restoreExperiment(id),
    onSuccess: () => {
      invalidate();
      toast(tr('已恢复', 'Restored'), 'ok');
    },
    onError: onErr,
  });
  const deleteOne = useMutation({
    mutationFn: (id: string) => api.deleteExperimentPermanent(id),
    onSuccess: () => {
      invalidate();
      toast(tr('已永久删除', 'Deleted permanently'), 'ok');
    },
    onError: onErr,
    onSettled: () => setConfirm(null),
  });
  const batch = useMutation({
    mutationFn: (v: { action: 'trash' | 'restore' | 'delete'; ids: string[] }) => api.batchExperiments(pid!, v.action, v.ids),
    onSuccess: (r, v) => {
      invalidate();
      setSelected(new Set());
      toast(
        v.action === 'trash'
          ? tr(`已将 ${r.affected} 个移入回收站`, `Moved ${r.affected} to trash`)
          : v.action === 'restore'
            ? tr(`已恢复 ${r.affected} 个`, `Restored ${r.affected}`)
            : tr(`已永久删除 ${r.affected} 个`, `Deleted ${r.affected} permanently`),
        'ok',
      );
    },
    onError: onErr,
    onSettled: () => setConfirm(null),
  });
  const emptyTrash = useMutation({
    mutationFn: () => api.emptyExperimentTrash(pid!),
    onSuccess: (r) => {
      invalidate();
      toast(tr(`已清空回收站，删除 ${r.affected} 个`, `Trash emptied: ${r.affected} deleted`), 'ok');
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

  /* —— 回收站 —— */
  if (view === 'trash') {
    return (
      <div className="page fadeup">
        <div className="exp-body">
          <button type="button" className="task-back ideas-backlink" onClick={() => { setView('active'); setSelecting(false); }}>
            ← {tr('实验', 'Experiments')}
          </button>
          <div className="ideas-head">
            <div className="ideas-head-text">
              <h2 className="ideas-lead">{tr('回收站', 'Trash')}</h2>
              <span className="ideas-note">{trash.length > 0 ? tr(`${trash.length} 个实验`, `${trash.length} ${trash.length === 1 ? 'experiment' : 'experiments'}`) : ''}</span>
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
                      message: tr(
                        `其中 ${trash.length} 个实验会被永久删除，无法恢复。`,
                        trash.length === 1 ? 'The experiment in it will be deleted permanently. This can’t be undone.' : `Its ${trash.length} experiments will be deleted permanently. This can’t be undone.`,
                      ),
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
              {trash.map((e) => (
                <div key={e.id} className="exp-row" data-exp={e.id}>
                  {selecting && <CheckBox checked={selected.has(e.id)} onToggle={() => toggle(e.id)} label={tr('选择', 'Select')} />}
                  <div className="exp-row-main">
                    <span className="dr-row-name">{e.idea_title}</span>
                    <span className="exp-row-state">
                      {tr('删除于', 'Deleted')} {e.trashed_at ? fmtRelative(e.trashed_at) : '—'}
                    </span>
                  </div>
                  {!selecting && (
                    <>
                      <button type="button" className="btn btn-ghost sm" disabled={restoreOne.isPending} onClick={() => restoreOne.mutate(e.id)}>
                        {tr('恢复', 'Restore')}
                      </button>
                      <button
                        type="button"
                        className="btn btn-danger-soft sm"
                        style={{ marginRight: 8 }}
                        onClick={() =>
                          setConfirm({
                            title: tr(`永久删除「${e.idea_title}」？`, `Delete “${e.idea_title}” permanently?`),
                            message: tr('删除后无法恢复。', 'This can’t be undone.'),
                            confirmText: tr('永久删除', 'Delete'),
                            run: () => deleteOne.mutate(e.id),
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
              <span className="ideas-batch-count">{tr(`已选 ${selected.size} 个`, `${selected.size} selected`)}</span>
              <button type="button" className="btn btn-ghost sm" disabled={batch.isPending} onClick={() => batch.mutate({ action: 'restore', ids: selectedIds })}>
                {tr('恢复', 'Restore')}
              </button>
              <button
                type="button"
                className="btn btn-danger-soft sm"
                onClick={() =>
                  setConfirm({
                    title: tr(`永久删除 ${selected.size} 个实验？`, selected.size === 1 ? 'Delete 1 experiment permanently?' : `Delete ${selected.size} experiments permanently?`),
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
  const waiting = experiments.filter(needsYou).length;
  const running = experiments.filter((e) => !EXPERIMENT_TERMINAL.has(e.status)).length;
  const lead = tr(`${experiments.length} 个实验`, `${experiments.length} ${experiments.length === 1 ? 'experiment' : 'experiments'}`);
  const note = [
    waiting > 0 ? tr(`${waiting} 个在等你`, `${waiting} waiting for you`) : '',
    running - waiting > 0 ? tr(`${running - waiting} 个进行中`, `${running - waiting} in progress`) : '',
  ]
    .filter(Boolean)
    .join(tr('，', ', '));

  const pageMenu: DotsMenuItem[] = [
    { key: 'select', label: selecting ? tr('退出批量管理', 'Stop selecting') : tr('批量管理', 'Select several'), icon: 'check', onSelect: () => { setSelecting((s) => !s); setSelected(new Set()); } },
    { key: 'machines', label: tr('远程机器', 'Remote machines'), icon: 'server', onSelect: () => navigate('/settings?tab=ssh') },
    { key: 'trash', label: trash.length > 0 ? tr(`回收站（${trash.length}）`, `Trash (${trash.length})`) : tr('回收站', 'Trash'), icon: 'trash', separated: true, onSelect: () => { setView('trash'); setSelecting(false); } },
  ];

  const ideaCount = (ideasQuery.data ?? []).filter((i) => i.status !== 'rejected').length;
  const machineCount = (credsQuery.data ?? []).length;
  const needsLoading = ideasQuery.isLoading || credsQuery.isLoading;

  return (
    <div className="page fadeup">
      <div className="exp-body">
        {activeQuery.isLoading ? (
          <LoadingState rows={3} />
        ) : activeQuery.isError ? (
          <ErrorState title={tr('无法加载实验，请重试', 'Couldn’t load experiments. Try again.')} detail={errorText(activeQuery.error)} onRetry={() => void activeQuery.refetch()} />
        ) : isEmpty ? (
          <div className="exp-needs" data-testid="exp-empty">
            <h2 className="exp-needs-title">{tr('还没有实验', 'No experiments yet')}</h2>
            <p className="exp-needs-lead">
              {tr(
                '实验是为一个想法做的：AI 在你的远程机器上搭环境、写代码、一轮一轮地运行，最后写出报告。开始前需要两样东西。',
                'An experiment tests one idea: the AI sets up on your remote machine, writes the code, runs it round by round and writes a report. It needs two things first.',
              )}
            </p>
            {needsLoading ? (
              <LoadingState compact rows={2} />
            ) : (
              <>
                <div className="exp-needs-list">
                  <div className={'exp-need' + (ideaCount > 0 ? ' is-ok' : '')} data-need="idea">
                    <span className="exp-need-mark">{ideaCount > 0 && <Icon name="check" size={10} sw={2.6} />}</span>
                    <div className="exp-need-text">
                      <div className="exp-need-name">{tr('一个想法', 'An idea')}</div>
                      <div className="exp-need-hint">
                        {ideaCount > 0
                          ? tr(`这个课题有 ${ideaCount} 个想法。`, `This topic has ${ideaCount} ${ideaCount === 1 ? 'idea' : 'ideas'}.`)
                          : tr('这个课题还没有想法，先去「想法」页生成。', 'This topic has no ideas yet. Generate some on the Ideas page first.')}
                      </div>
                    </div>
                  </div>
                  <div className={'exp-need' + (machineCount > 0 ? ' is-ok' : '')} data-need="machine">
                    <span className="exp-need-mark">{machineCount > 0 && <Icon name="check" size={10} sw={2.6} />}</span>
                    <div className="exp-need-text">
                      <div className="exp-need-name">{tr('一台远程机器', 'A remote machine')}</div>
                      <div className="exp-need-hint">
                        {machineCount > 0
                          ? tr(`已添加 ${machineCount} 台。实验文件放在机器的 ~/polaris_runs/ 下。`, `${machineCount} added. Experiment files go under ~/polaris_runs/ on the machine.`)
                          : tr('一台能用 SSH 连接的服务器（有 GPU 更好）。目前不能在本机上运行实验。', 'A server you can reach over SSH (ideally with a GPU). Experiments can’t run on this computer yet.')}
                      </div>
                    </div>
                    {machineCount === 0 && ideaCount === 0 && (
                      <Link className="btn btn-ghost sm" to="/settings?tab=ssh">
                        {tr('添加远程机器', 'Add a machine')}
                      </Link>
                    )}
                  </div>
                </div>
                <div className="exp-needs-action">
                  {ideaCount === 0 ? (
                    <Link className="btn btn-primary" to={topicPath(pid, 'forge')}>
                      {tr('去生成想法', 'Generate ideas')}
                    </Link>
                  ) : machineCount === 0 ? (
                    <Link className="btn btn-primary" to="/settings?tab=ssh">
                      {tr('添加远程机器', 'Add a remote machine')}
                    </Link>
                  ) : (
                    <button type="button" className="btn btn-primary" onClick={() => setModalOpen(true)}>
                      {tr('新建实验', 'New experiment')}
                    </button>
                  )}
                  {trash.length > 0 && (
                    <button type="button" className="btn btn-plain sm" onClick={() => setView('trash')}>
                      {tr(`回收站（${trash.length}）`, `Trash (${trash.length})`)}
                    </button>
                  )}
                </div>
              </>
            )}
          </div>
        ) : (
          <>
            <div className="ideas-head">
              <div className="ideas-head-text">
                <h2 className="ideas-lead">{lead}</h2>
                {note && <span className="ideas-note">{note}</span>}
              </div>
              <div className="ideas-head-actions">
                <button type="button" className="btn btn-primary sm" onClick={() => setModalOpen(true)}>
                  {tr('新建实验', 'New experiment')}
                </button>
                <DotsMenu items={pageMenu} />
              </div>
            </div>

            {selecting && (
              <div className="ideas-selectbar">
                <CheckBox
                  checked={experiments.every((e) => selected.has(e.id))}
                  onToggle={() => setSelected(experiments.every((e) => selected.has(e.id)) ? new Set() : new Set(experiments.map((e) => e.id)))}
                  label={tr('全选', 'Select all')}
                />
                <span>{selected.size > 0 ? tr(`已选 ${selected.size} 个`, `${selected.size} selected`) : tr('全选', 'Select all')}</span>
                <button type="button" className="btn btn-danger-soft sm" disabled={selected.size === 0 || batch.isPending} onClick={() => batch.mutate({ action: 'trash', ids: selectedIds })}>
                  {tr('移入回收站', 'Move to trash')}
                </button>
                <button type="button" className="btn btn-plain sm" onClick={() => { setSelecting(false); setSelected(new Set()); }}>
                  {tr('完成', 'Done')}
                </button>
              </div>
            )}

            <div className="dr-rows exp-rows" data-testid="exp-list">
              {experiments.map((e) => {
                const terminal = EXPERIMENT_TERMINAL.has(e.status);
                const need = needsYou(e);
                return (
                  <div key={e.id} className="exp-row" data-exp={e.id} data-status={e.status}>
                    {selecting && <CheckBox checked={selected.has(e.id)} onToggle={() => toggle(e.id)} label={tr('选择', 'Select')} />}
                    <Link className="exp-row-main" to={`/experiment/${e.id}`}>
                      <span className="dr-row-name" title={e.idea_title}>
                        {e.idea_title}
                      </span>
                      <span className="exp-row-state">
                        <span className={`task-dot is-${experimentTone(e.status)}`} aria-hidden="true" />
                        <span className={need ? 'is-need' : undefined}>{experimentSentence(e.status)}</span>
                        <span className="exp-row-meta">
                          {[e.server_host, elapsedText(e.created_at, terminal ? e.updated_at : null, now)].filter(Boolean).join(' · ')}
                        </span>
                      </span>
                    </Link>
                    {!selecting && (
                      <DotsMenu
                        plain
                        label={tr('更多操作', 'More actions')}
                        items={[
                          ...(e.idea_id ? [{ key: 'idea', label: tr('查看想法', 'View the idea'), icon: 'bulb', onSelect: () => navigate(`/ideas/${e.idea_id}`) } as DotsMenuItem] : []),
                          {
                            key: 'trash',
                            label: tr('移入回收站', 'Move to trash'),
                            icon: 'trash',
                            danger: true,
                            onSelect: () => trashOne.mutate(e.id),
                          },
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
      <NewExperimentModal open={modalOpen} onClose={closeModal} pid={pid} initialIdeaId={newIdeaId} />
    </div>
  );
}
