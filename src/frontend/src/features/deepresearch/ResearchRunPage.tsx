import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { ErrorState, LoadingState } from '../../components/ui/EmptyState';
import { Icon } from '../../components/ui/Icon';
import { PromptModal } from '../../components/ui/PromptModal';
import { toast } from '../../components/ui/Toast';
import { api } from '../../lib/api';
import { errorText } from '../../lib/errors';
import { fmtRelative } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { isEngineUnreachable, researchApi, type ResearchBrief, type RetryRunInput } from '../../lib/researchApi';
import { StatusDot } from '../voyages/shared/StatusDot';
import { useVoyageChannel } from '../voyages/shared/useVoyageChannel';
import { researchKeys, useNow, useResearchRun } from './hooks';
import { IdeasSection } from './IdeasSection';
import { NextSection } from './NextSection';
import { PapersSection } from './PapersSection';
import type { RunActions } from './parts';
import { countsText, elapsedText, runHeadline, sectionStates, usageText } from './phase';
import { ScopeSection } from './ScopeSection';
import './deepresearch.css';

/* ============================================================
   /research/:id —— 一次研究的时间线：范围 / 文献 / 想法 / 下一步。

   数据只有一份：GET /research/{id}，按状态轮询（见 hooks.ts）。当前子任务的事件流
   只做一件事：状态或步骤一变就提前取一次数。事件流断了、重连后漏了事件都没关系，
   下一次轮询会补上。页面上「正在做什么」的说法来自引擎报的当前步骤，不是日志行。
   ============================================================ */

export function ResearchRunPage() {
  const { id = '' } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const query = useResearchRun(id);
  const run = query.data;

  const [editing, setEditing] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);

  // 换到另一次研究：收起上一页留下的编辑器和菜单
  useEffect(() => {
    setEditing(false);
    setMenuOpen(false);
  }, [id]);

  useEffect(() => {
    if (!menuOpen) return;
    const onDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMenuOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [menuOpen]);

  // —— 提前取数：事件来了就把下一次轮询提到现在（合并 400ms 内的多次）——
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const refresh = useCallback(() => {
    if (refreshTimer.current) return;
    refreshTimer.current = setTimeout(() => {
      refreshTimer.current = null;
      void queryClient.invalidateQueries({ queryKey: researchKeys.run(id) });
      void queryClient.invalidateQueries({ queryKey: researchKeys.list });
    }, 400);
  }, [queryClient, id]);
  useEffect(
    () => () => {
      if (refreshTimer.current) clearTimeout(refreshTimer.current);
      refreshTimer.current = null;
    },
    [id],
  );

  // —— 当前子任务的事件流（只在还会变的时候订阅）——
  const currentVoyageId = run
    ? run.phase === 'survey'
      ? run.survey.voyage_id
      : run.phase === 'hypotheses'
        ? run.hypotheses.voyage_id
        : null
    : null;
  const live = !!run && (run.status === 'running' || run.status === 'waiting_user');
  useVoyageChannel(currentVoyageId, live, { onExtraEvent: refresh });
  // 事件流把 status / step 写进这份任务缓存；它一变就提前取一次研究数据
  const voyageQuery = useQuery({
    queryKey: ['voyage', currentVoyageId, false],
    queryFn: () => api.getVoyage(currentVoyageId ?? '', { includeObsolete: false }),
    enabled: !!currentVoyageId && live,
    retry: false,
    staleTime: 60_000,
  });
  const voyageSignature = voyageQuery.data
    ? `${voyageQuery.data.id}:${voyageQuery.data.status}:${(voyageQuery.data.steps ?? []).map((s) => s.status).join(',')}`
    : null;
  const lastSignature = useRef<string | null>(null);
  useEffect(() => {
    const prev = lastSignature.current;
    lastSignature.current = voyageSignature;
    if (prev && voyageSignature && prev !== voyageSignature && prev.split(':')[0] === voyageSignature.split(':')[0]) refresh();
  }, [voyageSignature, refresh]);

  const invalidate = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: researchKeys.run(id) });
    void queryClient.invalidateQueries({ queryKey: researchKeys.list });
  }, [queryClient, id]);

  const retry = useMutation({
    mutationFn: (input: RetryRunInput) => researchApi.retryRun(id, input),
    onSuccess: () => {
      setEditing(false);
      invalidate();
    },
    onError: (e) => toast(errorText(e), 'error'),
  });
  const more = useMutation({
    mutationFn: () => researchApi.moreIdeas(id),
    onSuccess: invalidate,
    onError: (e) => toast(errorText(e), 'error'),
  });
  const cancel = useMutation({
    mutationFn: () => researchApi.cancelRun(id),
    onSuccess: () => {
      setStopping(false);
      toast(tr('已停止', 'Stopped'), 'ok');
      invalidate();
    },
    onError: (e) => toast(errorText(e), 'error'),
  });
  const rename = useMutation({
    mutationFn: (name: string) => researchApi.renameRun(id, name),
    onSuccess: () => {
      setRenaming(false);
      invalidate();
      void queryClient.invalidateQueries({ queryKey: ['projects'] });
    },
    onError: (e) => toast(errorText(e), 'error'),
  });
  const remove = useMutation({
    mutationFn: () => researchApi.deleteRun(id, true),
    onSuccess: () => {
      setDeleting(false);
      toast(tr('已删除', 'Deleted'), 'ok');
      queryClient.removeQueries({ queryKey: researchKeys.run(id) });
      void queryClient.invalidateQueries({ queryKey: researchKeys.list });
      void queryClient.invalidateQueries({ queryKey: ['projects'] });
      void queryClient.invalidateQueries({ queryKey: ['libraries'] });
      navigate('/research', { replace: true });
    },
    onError: (e) => toast(errorText(e), 'error'),
  });

  const running = run?.status === 'running';
  const now = useNow(30_000, running);

  if (query.isLoading) {
    return (
      <div className="page dr-run">
        <LoadingState rows={5} />
      </div>
    );
  }

  if (!run) {
    const down = isEngineUnreachable(query.error);
    return (
      <div className="page dr-run">
        <ErrorState
          title={
            down
              ? tr('无法连接本机引擎，正在重试…', 'Couldn’t reach the local engine. Retrying…')
              : errorText(query.error)
          }
          onRetry={down ? () => void query.refetch() : undefined}
          action={
            <Link className="btn btn-ghost sm" to="/research">
              {tr('返回研究', 'Back to Research')}
            </Link>
          }
        />
      </div>
    );
  }

  const states = sectionStates(run);
  const head = runHeadline(run);
  const engineDown = query.isError && isEngineUnreachable(query.error);
  const stoppable = run.status === 'running' || run.status === 'waiting_user' || run.status === 'needs_attention';
  const actions: RunActions = {
    retry: (input) => retry.mutate(input ?? {}),
    editScope: () => setEditing(true),
    busy: retry.isPending || more.isPending || cancel.isPending,
    editing,
  };
  const usage = usageText(run.usage);
  // 运行中说已经跑了多久（从创建算起）；其余状态说最后一次变化是什么时候
  const when = running ? elapsedText(run.created_at, null, now) : fmtRelative(run.updated_at);
  const meta = [countsText(run.counts), when].filter(Boolean).join(' · ');
  // 名称就是方向原话（还没起名、或方向本来就短）时不重复一遍
  const showDirection = run.direction.trim() !== run.name.trim();

  return (
    <div className="page fadeup dr-run">
      {engineDown && (
        <div className="dr-banner" role="status">
          <StatusDot tone="warn" label={tr('无法连接本机引擎，正在重试…', 'Couldn’t reach the local engine. Retrying…')} />
          <span className="dr-banner-sub">{tr('下面是上一次取到的内容。', 'Showing the last known state.')}</span>
        </div>
      )}

      <header className="dr-run-head">
        <div className="dr-run-titlebar">
          <h1 className="dr-run-title" title={run.name}>
            {run.name}
          </h1>
          <div className="dr-menu-root" ref={menuRef}>
            <button
              type="button"
              className="icon-btn"
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              title={tr('更多', 'More')}
              aria-label={tr('更多', 'More')}
              onClick={() => setMenuOpen((o) => !o)}
            >
              <span className="dr-dots" aria-hidden="true">
                <i />
                <i />
                <i />
              </span>
            </button>
            {menuOpen && (
              <div className="dr-menu" role="menu">
                <button
                  type="button"
                  role="menuitem"
                  className="user-menu-item"
                  onClick={() => {
                    setMenuOpen(false);
                    setRenaming(true);
                  }}
                >
                  <Icon name="pen" size={14} />
                  {tr('重命名', 'Rename')}
                </button>
                {stoppable && (
                  <button
                    type="button"
                    role="menuitem"
                    className="user-menu-item"
                    onClick={() => {
                      setMenuOpen(false);
                      setStopping(true);
                    }}
                  >
                    <Icon name="pause" size={14} />
                    {tr('停止研究', 'Stop research')}
                  </button>
                )}
                <div className="user-menu-sep" />
                <button
                  type="button"
                  role="menuitem"
                  className="user-menu-item danger"
                  onClick={() => {
                    setMenuOpen(false);
                    setDeleting(true);
                  }}
                >
                  <Icon name="trash" size={14} />
                  {tr('删除课题', 'Delete topic')}
                </button>
              </div>
            )}
          </div>
        </div>
        {showDirection && (
          <p className="dr-run-direction" title={run.direction}>
            {run.direction}
          </p>
        )}
        <div className="dr-run-status" data-run-status={run.status} data-run-phase={run.phase}>
          <StatusDot tone={head.tone} label={head.label} />
          <span className="dr-run-meta">{meta}</span>
          {usage && (
            <span className="dr-run-meta" title={usage.title}>
              {usage.text}
            </span>
          )}
        </div>
      </header>

      <div className="dr-timeline">
        <ScopeSection
          run={run}
          state={states.scope}
          actions={actions}
          editing={editing}
          onCloseEdit={() => setEditing(false)}
          onSubmitEdit={(brief: ResearchBrief) => retry.mutate({ brief })}
        />
        <PapersSection run={run} state={states.papers} actions={actions} />
        <IdeasSection
          run={run}
          state={states.ideas}
          actions={actions}
          onMoreIdeas={() => more.mutate()}
          onChanged={invalidate}
        />
        <NextSection run={run} state={states.next} />
      </div>

      <PromptModal
        open={renaming}
        onClose={() => setRenaming(false)}
        title={tr('重命名课题', 'Rename topic')}
        initial={run.name}
        submitText={tr('保存', 'Save')}
        busy={rename.isPending}
        onSubmit={(name) => rename.mutate(name)}
      />
      <ConfirmModal
        open={stopping}
        onClose={() => setStopping(false)}
        title={tr('停止这次研究？', 'Stop this research?')}
        message={tr('已找到的论文和想法会保留，之后可以继续。', 'Papers and ideas found so far are kept. You can resume later.')}
        confirmText={tr('停止', 'Stop')}
        busy={cancel.isPending}
        onConfirm={() => cancel.mutate()}
      />
      <ConfirmModal
        open={deleting}
        onClose={() => setDeleting(false)}
        title={tr(`删除课题「${run.name}」？`, `Delete topic “${run.name}”?`)}
        message={tr(
          '课题、它的想法和文献库都会删除，无法恢复。',
          'The topic, its ideas and its library are deleted. This can’t be undone.',
        )}
        confirmText={tr('删除', 'Delete')}
        danger
        busy={remove.isPending}
        onConfirm={() => remove.mutate()}
      />
    </div>
  );
}
