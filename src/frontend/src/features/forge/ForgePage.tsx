import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { DotsMenu, type DotsMenuItem } from '../../components/ui/DotsMenu';
import { EmptyState, ErrorState, LoadingState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { topicPath, useProject } from '../../app/project';
import { useShell } from '../../app/AppShell';
import { fmtRelative } from '../../lib/format';
import { errorText } from '../../lib/errors';
import { tr } from '../../lib/i18n';
import { api, type IdeaRead, type IdeaSort, type VoyageRead } from '../../lib/api';
import { useDirections } from '../deepresearch/directions';
import { researchKeys, useResearchRun } from '../deepresearch/hooks';
import { IdeaEvidence } from '../deepresearch/IdeaEvidence';
import { evidenceText } from '../deepresearch/phase';
import { CompareModal, comparePlan } from './CompareModal';
import { DeepDiveDrawer } from './DeepDiveDrawer';
import { GenerateIdeasModal } from './GenerateIdeasModal';
import { ParkedIdeaRunBanner } from './ParkedIdeaRunBanner';
import { originKind, originLabel, rankIdeas, rankText, recordText, scoresLine, standingText, unsupportedOf, type EvidenceCounts, type IdeaRank } from './ideaView';
import '../deepresearch/deepresearch.css';
import '../voyages/task.css';
import './ideas.css';

/* ============================================================
   课题的「想法」页（/t/:id/forge）。

   一页说清三件事：这个课题有哪些想法（和时间线上同一种卡片：标题、一段话、
   文献依据的数量、安静的分数、是否选定、怎么来的）；哪个更值得做（对比后的名次
   直接写在卡片上）；以及怎么再要一些（唯一的主按钮「再想一些」）。

   以前这里有两个生成按钮、一个草案/方案筛选、四个分数环和一个评分数字，
   「想法评审」还是另一页。现在生成只有一个入口，对比排序是这里的一个次要动作，
   结果回到这张列表上；辩论记录在每个想法的详情里。
   ============================================================ */

type Filter = 'all' | 'picked' | 'rejected';
type View = 'active' | 'trash';

const IDEA_TASK_KINDS = new Set(['idea_forge', 'idea_review', 'idea_proposal', 'discovery']);
const IN_FLIGHT = new Set(['planning', 'executing', 'verifying', 'replanning']);

/** 正在跑的想法类任务在做什么，一句话。 */
function taskSentence(kind: string): string {
  if (kind === 'idea_review') return tr('正在对比排序', 'Comparing ideas');
  if (kind === 'idea_proposal') return tr('正在写研究方案', 'Writing a proposal');
  if (kind === 'discovery') return tr('正在提出假设并对照文献核对', 'Proposing hypotheses and checking them against the papers');
  return tr('正在生成想法', 'Generating ideas');
}

function CheckBox({ checked, onToggle, label }: { checked: boolean; onToggle: () => void; label: string }) {
  return (
    <button type="button" role="checkbox" aria-checked={checked} aria-label={label} title={label} className={'ideas-check' + (checked ? ' is-on' : '')} onClick={onToggle}>
      {checked && <Icon name="check" size={12} sw={2.4} />}
    </button>
  );
}

interface IdeaRowProps {
  idea: IdeaRead;
  rank: IdeaRank | undefined;
  evidence: EvidenceCounts | null;
  /** 刚生成出来的 */
  fresh: boolean;
  busy: boolean;
  selecting: boolean;
  selected: boolean;
  onToggleSelect: () => void;
  onPick: (picked: boolean) => void;
  menu: DotsMenuItem[];
}

function IdeaRow({ idea, rank, evidence, fresh, busy, selecting, selected, onToggleSelect, onPick, menu }: IdeaRowProps) {
  const [open, setOpen] = useState(false);
  const picked = idea.status === 'promoted';
  const rejected = idea.status === 'rejected';
  const scores = scoresLine(idea.scores);
  const origin = originLabel(originKind(idea));
  return (
    <li className={'dr-idea ideas-row' + (picked ? ' is-picked' : '') + (rejected ? ' is-rejected' : '')} data-idea={idea.id} data-status={idea.status}>
      {selecting && <CheckBox checked={selected} onToggle={onToggleSelect} label={selected ? tr('取消选择', 'Deselect') : tr('选择', 'Select')} />}
      <div className="dr-idea-main">
        <Link to={`/ideas/${idea.id}`} className="dr-idea-title" title={idea.title}>
          {idea.title}
        </Link>
        <p className="dr-idea-summary">{idea.summary}</p>
        <div className="dr-idea-meta">
          {fresh && <span className="ideas-new">{tr('新', 'New')}</span>}
          {evidence && (
            <button type="button" className="dr-idea-evidence" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
              {evidenceText(evidence)}
              <Icon name="chevDown" size={11} style={{ transform: open ? 'rotate(180deg)' : undefined }} />
            </button>
          )}
          {rank && (
            <span className="ideas-rank" title={`${rankText(rank)} · ${recordText(rank)}`}>
              {standingText(rank)}
            </span>
          )}
          {scores && <span className="dr-idea-scores">{scores}</span>}
          {origin && <span className="dr-idea-scores">{origin}</span>}
          {rejected && <span className="dr-idea-scores">{tr('已淘汰', 'Rejected')}</span>}
        </div>
        {open && evidence && <IdeaEvidence ideaId={idea.id} unsupported={evidence.unsupported} />}
      </div>
      {!selecting && (
        <div className="dr-idea-act ideas-row-act">
          {rejected ? null : picked ? (
            <>
              <span className="dr-idea-picked">
                <Icon name="check" size={12} sw={2.2} />
                {tr('已选定', 'Picked')}
              </span>
              <button type="button" className="btn btn-plain xs" disabled={busy} onClick={() => onPick(false)}>
                {tr('取消选定', 'Unpick')}
              </button>
            </>
          ) : (
            <button type="button" className="btn btn-ghost sm" disabled={busy} onClick={() => onPick(true)}>
              {tr('选定', 'Pick')}
            </button>
          )}
          <DotsMenu items={menu} plain label={tr('更多操作', 'More actions')} />
        </div>
      )}
    </li>
  );
}

export function ForgePage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { openGates } = useShell();
  const { currentProject, currentProjectId: pid } = useProject();
  const { byProject } = useDirections();
  const run = byProject(pid)?.run ?? null;

  const [view, setView] = useState<View>('active');
  const [filter, setFilter] = useState<Filter>('all');
  const [sort, setSort] = useState<IdeaSort | null>(null);
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [generateOpen, setGenerateOpen] = useState(false);
  const [compareOpen, setCompareOpen] = useState(false);
  const [proposalOpen, setProposalOpen] = useState(false);
  const [confirm, setConfirm] = useState<null | { title: string; message: string; confirmText: string; run: () => void }>(null);

  /* —— 数据 —— */
  const ideasQuery = useQuery({
    queryKey: ['ideas', pid, 'all'],
    queryFn: () => api.listIdeas(pid!),
    enabled: !!pid,
    retry: false,
  });
  const trashQuery = useQuery({
    queryKey: ['ideas', pid, 'trash'],
    queryFn: () => api.listIdeas(pid!, { trashed: true }),
    enabled: !!pid,
    retry: false,
  });
  const leaderboardQuery = useQuery({
    queryKey: ['leaderboard', pid],
    queryFn: () => api.getLeaderboard(pid!),
    enabled: !!pid,
    retry: false,
  });
  const voyagesQuery = useQuery({
    queryKey: ['voyages', pid],
    queryFn: () => api.listVoyages(pid!),
    enabled: !!pid,
    retry: false,
    refetchInterval: (q) => ((q.state.data ?? []).some((v) => IDEA_TASK_KINDS.has(v.kind) && IN_FLIGHT.has(v.status)) ? 4_000 : 30_000),
  });
  const stateQuery = useQuery({
    queryKey: ['forge-state', pid],
    queryFn: () => api.getForgeState(pid!),
    enabled: !!pid,
    retry: false,
    refetchInterval: (q) => (q.state.data?.running_voyage_id || q.state.data?.parked_run?.retry_at ? 5_000 : 60_000),
  });
  const deepQuery = useQuery({
    queryKey: ['deep-state', pid],
    queryFn: () => api.getDeepIdeaState(pid!),
    enabled: !!pid,
    retry: false,
    refetchInterval: (q) => (q.state.data?.pending_gate_id ? 5_000 : 60_000),
  });
  const tournamentQuery = useQuery({
    queryKey: ['latest-tournament', pid],
    queryFn: () => api.getLatestTournamentSummary(pid!),
    enabled: !!pid,
    retry: false,
  });
  const librariesQuery = useQuery({
    queryKey: ['sourceLibraries', pid],
    queryFn: () => api.getSourceLibraries(pid!),
    enabled: !!pid && !run,
    retry: false,
  });
  const statsQuery = useQuery({
    queryKey: ['stats', pid],
    queryFn: () => api.getStats(pid!),
    enabled: !!pid && !run,
    retry: false,
  });
  // 有研究进展的课题：文献依据的数量在那次研究的读数里
  const runQuery = useResearchRun(run?.id ?? '');

  const all = ideasQuery.data ?? [];
  const trash = trashQuery.data ?? [];
  const parked = stateQuery.data?.parked_run ?? null;
  const pendingGate = deepQuery.data?.pending_gate_id ?? null;
  const tournament = tournamentQuery.data ?? null;

  const running: VoyageRead | null = useMemo(
    () => (voyagesQuery.data ?? []).find((v) => IDEA_TASK_KINDS.has(v.kind) && IN_FLIGHT.has(v.status)) ?? null,
    [voyagesQuery.data],
  );

  // 任务跑完的那一刻：把列表、名次、上次对比都重新取一遍；
  // 生成类的任务还要说清楚这次多了几个想法（一个都没多也要说）
  const wasRunning = useRef<{ id: string; kind: string; ids: Set<string> } | null>(null);
  const [outcome, setOutcome] = useState<{ ids: Set<string>; kind: string; voyageId: string } | null>(null);
  useEffect(() => setOutcome(null), [pid]);
  useEffect(() => {
    const now = running?.id ?? null;
    const prev = wasRunning.current;
    if (running && ideasQuery.data && (!prev || prev.id !== running.id)) {
      wasRunning.current = { id: running.id, kind: running.kind, ids: new Set((ideasQuery.data ?? []).map((i) => i.id)) };
      setOutcome(null);
      return;
    }
    if (prev && !now) {
      wasRunning.current = null;
      void queryClient
        .fetchQuery({ queryKey: ['ideas', pid, 'all'], queryFn: () => api.listIdeas(pid!), staleTime: 0 })
        .then((list) => {
          if (prev.kind === 'idea_review') return;
          // 哪些是这次新写的：引擎记了来源任务就按它认，旧引擎不记，才用前后两份列表相减
          const mine = list.filter((i) => i.source_voyage_id === prev.id);
          const fresh = mine.length > 0 ? mine : list.filter((i) => !prev.ids.has(i.id));
          setOutcome({ ids: new Set(fresh.map((i) => i.id)), kind: prev.kind, voyageId: prev.id });
        })
        .catch(() => {});
      void queryClient.invalidateQueries({ queryKey: ['leaderboard', pid] });
      void queryClient.invalidateQueries({ queryKey: ['latest-tournament', pid] });
      void queryClient.invalidateQueries({ queryKey: ['forge-state', pid] });
      void queryClient.invalidateQueries({ queryKey: ['deep-state', pid] });
      if (run) void queryClient.invalidateQueries({ queryKey: researchKeys.run(run.id) });
    }
  }, [running, pid, run, queryClient, ideasQuery.data]);

  // 这次任务到底写了几个、因为重复丢了几个：任务结束后引擎在任务上给出结果
  const finishedQuery = useQuery({
    queryKey: ['voyage', outcome?.voyageId ?? '', 'result'],
    queryFn: () => api.getVoyage(outcome!.voyageId, { includeObsolete: false }),
    enabled: !!outcome && outcome.kind !== 'idea_review',
    retry: false,
  });
  const result = finishedQuery.data?.result ?? null;
  const added = result ? result.ideas_written : (outcome?.ids.size ?? 0);
  const dropped = result?.dropped_as_duplicates ?? 0;

  const ranks = useMemo(() => rankIdeas(leaderboardQuery.data ?? []), [leaderboardQuery.data]);
  const hasRanks = ranks.size > 0;
  const effectiveSort: IdeaSort = sort ?? (hasRanks ? 'elo' : 'score');

  const evidenceById = useMemo(() => {
    const map = new Map<string, EvidenceCounts>();
    for (const i of runQuery.data?.ideas ?? []) map.set(i.id, i.evidence_counts);
    return map;
  }, [runQuery.data]);
  const evidenceOf = (idea: IdeaRead): EvidenceCounts | null => {
    // 列表本身带数量（每个想法都有）；全是 0 的是没逐条核对过的想法，不显示这一项
    const own = idea.evidence_counts;
    if (own) return own.support + own.refute + own.unsupported > 0 ? own : null;
    // 旧引擎：有研究进展的课题从那次研究的读数里取，其余只知道有几处尚无依据
    const fromRun = evidenceById.get(idea.id);
    if (fromRun) return fromRun;
    const unsupported = unsupportedOf(idea).length;
    return idea.origin?.kind === 'discovery' && unsupported > 0 ? { support: 0, refute: 0, unsupported } : null;
  };

  const counts = useMemo(
    () => ({
      all: all.filter((i) => i.status !== 'rejected').length,
      picked: all.filter((i) => i.status === 'promoted').length,
      rejected: all.filter((i) => i.status === 'rejected').length,
    }),
    [all],
  );

  const shown = useMemo(() => {
    const base =
      filter === 'picked' ? all.filter((i) => i.status === 'promoted') : filter === 'rejected' ? all.filter((i) => i.status === 'rejected') : all.filter((i) => i.status !== 'rejected');
    const overall = (i: IdeaRead) => (i.scores ? (i.scores.novelty + i.scores.feasibility + i.scores.operability + i.scores.impact) / 4 : -1);
    const byNew = (a: IdeaRead, b: IdeaRead) => b.created_at.localeCompare(a.created_at);
    const sorted = [...base];
    if (effectiveSort === '-created_at') sorted.sort(byNew);
    else if (effectiveSort === 'score') sorted.sort((a, b) => overall(b) - overall(a) || byNew(a, b));
    else {
      // 名次：对比过的在前（按名次），没对比过的按综合评分接在后面
      sorted.sort((a, b) => {
        const ra = ranks.get(a.id);
        const rb = ranks.get(b.id);
        if (ra && rb) return ra.rank - rb.rank || rb.wins - ra.wins || overall(b) - overall(a);
        if (ra) return -1;
        if (rb) return 1;
        return overall(b) - overall(a) || byNew(a, b);
      });
    }
    return sorted;
  }, [all, filter, effectiveSort, ranks]);

  const list = view === 'trash' ? trash : shown;
  useEffect(() => setSelected(new Set()), [view, filter]);
  // 筛选落空（比如取消了最后一个选定）时回到全部
  useEffect(() => {
    if (filter === 'picked' && counts.picked === 0) setFilter('all');
    if (filter === 'rejected' && counts.rejected === 0) setFilter('all');
  }, [filter, counts]);

  /* —— 动作 —— */
  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['ideas', pid] });
    void queryClient.invalidateQueries({ queryKey: ['forge-state', pid] });
    void queryClient.invalidateQueries({ queryKey: ['leaderboard', pid] });
    void queryClient.invalidateQueries({ queryKey: researchKeys.list });
    if (run) void queryClient.invalidateQueries({ queryKey: researchKeys.run(run.id) });
  };
  const onErr = (e: unknown) => toast(errorText(e), 'error');

  const setStatus = useMutation({
    mutationFn: (v: { id: string; status: 'candidate' | 'promoted' | 'rejected' }) =>
      v.status === 'promoted' ? api.pickIdea(v.id) : api.patchIdea(v.id, { status: v.status }),
    // 先改界面再等回应：选定是高频、可逆的动作
    onMutate: async (v) => {
      const key = ['ideas', pid, 'all'];
      await queryClient.cancelQueries({ queryKey: key });
      const prev = queryClient.getQueryData<IdeaRead[]>(key);
      if (prev) queryClient.setQueryData<IdeaRead[]>(key, prev.map((i) => (i.id === v.id ? { ...i, status: v.status } : i)));
      return { prev };
    },
    onSuccess: (_d, v) => {
      if (v.status === 'promoted') toast(tr('已选定', 'Picked'), 'ok');
      else if (v.status === 'rejected') toast(tr('已淘汰', 'Rejected'), 'ok');
    },
    onError: (e, _v, ctx) => {
      if (ctx?.prev) queryClient.setQueryData(['ideas', pid, 'all'], ctx.prev);
      onErr(e);
    },
    onSettled: (_d, _e, v) => {
      invalidate();
      void queryClient.invalidateQueries({ queryKey: ['idea', v.id] });
    },
  });

  const expand = useMutation({
    mutationFn: (id: string) => api.startDeepIdea(pid!, { seed: { type: 'idea', value: id } }),
    onSuccess: () => {
      toast(tr('已开始写研究方案，写好后会出现在这里', 'Started writing the proposal. It will appear here when it’s done.'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['voyages'] });
      void queryClient.invalidateQueries({ queryKey: ['deep-state', pid] });
    },
    onError: onErr,
  });
  const trashOne = useMutation({
    mutationFn: (id: string) => api.trashIdea(id),
    onSuccess: () => {
      invalidate();
      toast(tr('已移入回收站', 'Moved to trash'), 'ok');
    },
    onError: onErr,
  });
  const restoreOne = useMutation({
    mutationFn: (id: string) => api.restoreIdea(id),
    onSuccess: () => {
      invalidate();
      toast(tr('已恢复', 'Restored'), 'ok');
    },
    onError: onErr,
  });
  const deleteOne = useMutation({
    mutationFn: (id: string) => api.deleteIdeaPermanent(id),
    onSuccess: () => {
      invalidate();
      toast(tr('已永久删除', 'Deleted permanently'), 'ok');
    },
    onError: onErr,
    onSettled: () => setConfirm(null),
  });
  const batch = useMutation({
    mutationFn: (v: { action: 'trash' | 'restore' | 'delete'; ids: string[] }) => api.batchIdeas(pid!, v.action, v.ids),
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
    mutationFn: () => api.emptyIdeaTrash(pid!),
    onSuccess: (r) => {
      invalidate();
      toast(tr(`已清空回收站，删除 ${r.affected} 个`, `Trash emptied: ${r.affected} deleted`), 'ok');
    },
    onError: onErr,
    onSettled: () => setConfirm(null),
  });
  const retryCompare = useMutation({
    mutationFn: () => api.retryFailedTournamentMatches(pid!),
    onSuccess: () => {
      toast(tr('已开始重试', 'Retry started'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['voyages'] });
      void queryClient.invalidateQueries({ queryKey: ['latest-tournament', pid] });
    },
    onError: onErr,
  });

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const selectedIds = list.filter((i) => selected.has(i.id)).map((i) => i.id);

  const rowMenu = (idea: IdeaRead): DotsMenuItem[] => {
    const rejected = idea.status === 'rejected';
    const items: DotsMenuItem[] = [];
    if (!rejected) {
      if (idea.depth !== 'proposal') {
        items.push({
          key: 'expand',
          label: tr('展开为研究方案', 'Expand into a proposal'),
          icon: 'file',
          disabled: !!running || expand.isPending,
          title: running ? tr('等正在运行的想法任务完成', 'Wait for the running idea task to finish') : undefined,
          onSelect: () => expand.mutate(idea.id),
        });
      }
      items.push({
        key: 'experiment',
        label: tr('开始实验', 'Start experiment'),
        icon: 'flask',
        onSelect: () => navigate(topicPath(pid, `experiment?new=${idea.id}`)),
      });
      items.push({ key: 'reject', label: tr('淘汰', 'Reject'), icon: 'x', separated: true, onSelect: () => setStatus.mutate({ id: idea.id, status: 'rejected' }) });
    } else {
      items.push({ key: 'restore', label: tr('恢复', 'Restore'), icon: 'refresh', onSelect: () => setStatus.mutate({ id: idea.id, status: 'candidate' }) });
    }
    items.push({ key: 'trash', label: tr('移入回收站', 'Move to trash'), icon: 'trash', danger: true, onSelect: () => trashOne.mutate(idea.id) });
    return items;
  };

  const pageMenu: DotsMenuItem[] = [
    { key: 'select', label: selecting ? tr('退出批量管理', 'Stop selecting') : tr('批量管理', 'Select several'), icon: 'check', onSelect: () => { setSelecting((s) => !s); setSelected(new Set()); } },
    { key: 'history', label: tr('对比记录', 'Comparison history'), icon: 'scale', onSelect: () => navigate(topicPath(pid, 'review')) },
    { key: 'tasks', label: tr('想法任务', 'Idea tasks'), icon: 'clock', onSelect: () => navigate(topicPath(pid, 'tasks')) },
    { key: 'trash', label: trash.length > 0 ? tr(`回收站（${trash.length}）`, `Trash (${trash.length})`) : tr('回收站', 'Trash'), icon: 'trash', separated: true, onSelect: () => { setView('trash'); setSelecting(false); } },
  ];

  const plan = comparePlan(all);
  const direction = currentProject?.statement?.trim() || currentProject?.name || '';
  const libraryId = run?.library_id ?? librariesQuery.data?.[0]?.id ?? null;
  const paperCount = run ? run.counts.papers : (statsQuery.data?.papers_total ?? null);
  const showNovelty = all.some((i) => i.origin?.novelty_scope === 'library');
  const isEmpty = !ideasQuery.isLoading && !ideasQuery.isError && all.length === 0;

  if (!pid) return null;

  /* —— 回收站 —— */
  if (view === 'trash') {
    return (
      <div className="page fadeup">
        <div className="ideas-body">
          <button type="button" className="task-back ideas-backlink" onClick={() => setView('active')}>
            ← {tr('想法', 'Ideas')}
          </button>
          <div className="ideas-head">
            <div className="ideas-head-text">
              <h2 className="ideas-lead">{tr('回收站', 'Trash')}</h2>
              <span className="ideas-note">
                {trash.length > 0 ? tr(`${trash.length} 个想法`, `${trash.length} ${trash.length === 1 ? 'idea' : 'ideas'}`) : ''}
              </span>
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
                      message: tr(`其中 ${trash.length} 个想法会被永久删除，无法恢复。`, trash.length === 1 ? 'The idea in it will be deleted permanently. This can’t be undone.' : `Its ${trash.length} ideas will be deleted permanently. This can’t be undone.`),
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
            <ul className="dr-ideas">
              {trash.map((idea) => (
                <li key={idea.id} className="dr-idea ideas-row" data-idea={idea.id}>
                  {selecting && <CheckBox checked={selected.has(idea.id)} onToggle={() => toggle(idea.id)} label={tr('选择', 'Select')} />}
                  <div className="dr-idea-main">
                    <div className="dr-idea-title">{idea.title}</div>
                    <p className="dr-idea-summary">{idea.summary}</p>
                    <div className="dr-idea-meta">
                      <span className="dr-idea-scores">{tr('删除于', 'Deleted')} {idea.trashed_at ? fmtRelative(idea.trashed_at) : '—'}</span>
                    </div>
                  </div>
                  {!selecting && (
                    <div className="dr-idea-act ideas-row-act">
                      <button type="button" className="btn btn-ghost sm" disabled={restoreOne.isPending} onClick={() => restoreOne.mutate(idea.id)}>
                        {tr('恢复', 'Restore')}
                      </button>
                      <button
                        type="button"
                        className="btn btn-danger-soft sm"
                        onClick={() =>
                          setConfirm({
                            title: tr(`永久删除「${idea.title}」？`, `Delete “${idea.title}” permanently?`),
                            message: tr('删除后无法恢复。', 'This can’t be undone.'),
                            confirmText: tr('永久删除', 'Delete'),
                            run: () => deleteOne.mutate(idea.id),
                          })
                        }
                      >
                        {tr('永久删除', 'Delete')}
                      </button>
                    </div>
                  )}
                </li>
              ))}
            </ul>
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
                    title: tr(`永久删除 ${selected.size} 个想法？`, selected.size === 1 ? 'Delete 1 idea permanently?' : `Delete ${selected.size} ideas permanently?`),
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
      </div>
    );
  }

  /* —— 想法列表 —— */
  const lead =
    counts.picked > 0
      ? tr(`${counts.all} 个想法，已选定 ${counts.picked} 个`, `${counts.all} ${counts.all === 1 ? 'idea' : 'ideas'}, ${counts.picked} picked`)
      : tr(`${counts.all} 个想法`, `${counts.all} ${counts.all === 1 ? 'idea' : 'ideas'}`);

  const compareLine = (() => {
    if (!tournament || running?.kind === 'idea_review') return null;
    if (tournament.failed > 0) {
      return (
        <div className="ideas-compare is-warn">
          <span>
            {tr(`上次对比有 ${tournament.failed} 场没完成（共 ${tournament.planned} 场）`, `${tournament.failed} of ${tournament.planned} comparisons didn’t finish last time`)}
          </span>
          {tournament.can_retry && (
            <button type="button" className="btn btn-ghost xs" disabled={retryCompare.isPending || !!running} onClick={() => retryCompare.mutate()}>
              {tr('重试没完成的', 'Retry unfinished')}
            </button>
          )}
          <Link to={topicPath(pid, 'review')}>{tr('对比记录', 'Comparison history')}</Link>
        </div>
      );
    }
    if (!hasRanks) return null;
    return (
      <div className="ideas-compare">
        <span>
          {tr(`名次来自 ${tournament.completed} 场两两对比。`, `Ranks come from ${tournament.completed} pairwise ${tournament.completed === 1 ? 'comparison' : 'comparisons'}.`)}
        </span>
        <Link to={topicPath(pid, 'review')}>{tr('查看对比记录', 'See the comparisons')}</Link>
      </div>
    );
  })();

  return (
    <div className="page fadeup">
      <div className="ideas-body">
        {running && (
          <div className="ideas-status" data-testid="ideas-running" data-kind={running.kind}>
            <span className="task-dot is-active" />
            <span className="ideas-status-text">{taskSentence(running.kind)}</span>
            <span className="ideas-status-meta">{tr('完成后这里会自动更新', 'This page updates when it’s done')}</span>
            <Link className="btn btn-plain xs" to={running.kind === 'discovery' && run ? `/research/${run.id}` : `/voyages/${running.id}`}>
              {tr('查看进度', 'View progress')}
            </Link>
          </div>
        )}
        {!running && outcome && (
          <div className="ideas-status is-quiet" data-testid="ideas-outcome" data-added={added} data-dropped={dropped} data-from-result={result ? 'yes' : 'no'}>
            <span className="ideas-status-text">
              {added > 0
                ? tr(`新增 ${added} 个想法`, `${added} new ${added === 1 ? 'idea' : 'ideas'}`)
                : tr('这次没有留下新的想法', 'No new ideas this time')}
            </span>
            {added > 0 && dropped > 0 && (
              <span className="ideas-status-meta">
                {tr(`另有 ${dropped} 个和已有的想法重复，没有保留。`, `${dropped} more repeated existing ideas and ${dropped === 1 ? 'was' : 'were'} not kept.`)}
              </span>
            )}
            {added === 0 && (
              <span className="ideas-status-meta">
                {dropped > 0
                  ? tr(`提出的 ${dropped} 个都和已有的想法重复。`, `All ${dropped} proposed repeated existing ideas.`)
                  : outcome.kind === 'discovery'
                    ? tr('提出的假设和已有的重复，或没通过文献核对。', 'The hypotheses repeated existing ones or didn’t hold against the papers.')
                    : tr('提出的想法和已有的太相似，被合并了。', 'What was proposed was too close to existing ideas and was merged.')}
              </span>
            )}
            <button type="button" className="btn btn-plain xs" onClick={() => setOutcome(null)}>
              {tr('知道了', 'Dismiss')}
            </button>
          </div>
        )}
        {parked && !(parked.status === 'paused_gate' && pendingGate) && <ParkedIdeaRunBanner run={parked} pid={pid} />}
        {pendingGate && (
          <div className="ideas-status is-warn">
            <span className="task-dot is-warn" />
            <span className="ideas-status-text">{tr('研究目标写好了，等你确认后再起草方案', 'The research goal is ready. Confirm it to start drafting the proposal.')}</span>
            <button type="button" className="btn btn-ghost xs" onClick={() => openGates(pendingGate)}>
              {tr('查看并确认', 'Review and confirm')}
            </button>
          </div>
        )}

        {ideasQuery.isLoading ? (
          <LoadingState rows={4} />
        ) : ideasQuery.isError ? (
          <ErrorState title={tr('无法加载想法，请重试', 'Couldn’t load ideas. Try again.')} detail={errorText(ideasQuery.error)} onRetry={() => void ideasQuery.refetch()} />
        ) : isEmpty ? (
          <div className="ideas-empty">
            <EmptyState
              icon="bulb"
              title={tr('还没有想法', 'No ideas yet')}
              desc={
                paperCount === 0
                  ? tr('想法从文献库里的论文提出。这个课题还没有论文，先在「概览」里开始研究。', 'Ideas come from the papers in your library. This topic has none yet; start the research from Overview first.')
                  : tr('想法从文献库里的论文提出，每个都写明有哪些文献依据。', 'Ideas come from the papers in your library, each with the evidence behind it.')
              }
              action={
                paperCount === 0 ? (
                  <Link className="btn btn-primary" style={{ textDecoration: 'none' }} to={topicPath(pid)}>
                    {tr('去概览', 'Go to Overview')}
                  </Link>
                ) : (
                  <button type="button" className="btn btn-primary" disabled={!!running} onClick={() => setGenerateOpen(true)}>
                    {tr('生成想法', 'Generate ideas')}
                  </button>
                )
              }
            />
            {trash.length > 0 && (
              <button type="button" className="btn btn-plain sm" onClick={() => setView('trash')}>
                {tr(`回收站（${trash.length}）`, `Trash (${trash.length})`)}
              </button>
            )}
          </div>
        ) : (
          <>
            <div className="ideas-head">
              <div className="ideas-head-text">
                <h2 className="ideas-lead">{lead}</h2>
                {showNovelty && <span className="ideas-note">{tr('评分满分 10；新颖性只对照你的文献库判断。', 'Scores are out of 10. Novelty is judged against your library only.')}</span>}
              </div>
              <div className="ideas-head-actions">
                {plan.matches > 0 && (
                  <button
                    type="button"
                    className="btn btn-ghost sm"
                    disabled={!!running}
                    title={running ? tr('等正在运行的想法任务完成', 'Wait for the running idea task to finish') : undefined}
                    onClick={() => setCompareOpen(true)}
                  >
                    {hasRanks ? tr('重新对比', 'Compare again') : tr('对比排序', 'Compare')}
                  </button>
                )}
                <button
                  type="button"
                  className="btn btn-primary sm"
                  disabled={!!running}
                  title={running ? tr('等正在运行的想法任务完成', 'Wait for the running idea task to finish') : undefined}
                  onClick={() => setGenerateOpen(true)}
                >
                  {tr('再想一些', 'Generate more')}
                </button>
                <DotsMenu items={pageMenu} />
              </div>
            </div>

            <div className="ideas-tools">
              <div className="ideas-filters" role="tablist" aria-label={tr('筛选', 'Filter')}>
                {(
                  [
                    { v: 'all', label: tr('全部', 'All'), n: counts.all, show: true },
                    { v: 'picked', label: tr('已选定', 'Picked'), n: counts.picked, show: counts.picked > 0 },
                    { v: 'rejected', label: tr('已淘汰', 'Rejected'), n: counts.rejected, show: counts.rejected > 0 },
                  ] as { v: Filter; label: string; n: number; show: boolean }[]
                )
                  .filter((f) => f.show)
                  .map((f) => (
                    <button key={f.v} type="button" role="tab" aria-selected={filter === f.v} className={'ideas-filter' + (filter === f.v ? ' is-on' : '')} onClick={() => setFilter(f.v)}>
                      {f.label}
                      <span className="ideas-filter-n">{f.n}</span>
                    </button>
                  ))}
              </div>
              <label className="ideas-sort">
                <span>{tr('排序', 'Sort')}</span>
                <select className="input" value={effectiveSort} onChange={(e) => setSort(e.target.value as IdeaSort)} aria-label={tr('排序', 'Sort')}>
                  {hasRanks && <option value="elo">{tr('对比名次', 'Comparison rank')}</option>}
                  <option value="score">{tr('综合评分', 'Overall score')}</option>
                  <option value="-created_at">{tr('最新', 'Newest')}</option>
                </select>
              </label>
            </div>

            {compareLine}

            {selecting && (
              <div className="ideas-selectbar">
                <CheckBox
                  checked={list.length > 0 && list.every((i) => selected.has(i.id))}
                  onToggle={() => setSelected(list.every((i) => selected.has(i.id)) ? new Set() : new Set(list.map((i) => i.id)))}
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

            <ul className="dr-ideas ideas-list">
              {shown.map((idea) => (
                <IdeaRow
                  key={idea.id}
                  idea={idea}
                  rank={ranks.get(idea.id)}
                  evidence={evidenceOf(idea)}
                  fresh={!!outcome?.ids.has(idea.id)}
                  busy={setStatus.isPending && setStatus.variables?.id === idea.id}
                  selecting={selecting}
                  selected={selected.has(idea.id)}
                  onToggleSelect={() => toggle(idea.id)}
                  onPick={(p) => setStatus.mutate({ id: idea.id, status: p ? 'promoted' : 'candidate' })}
                  menu={rowMenu(idea)}
                />
              ))}
            </ul>
          </>
        )}
      </div>

      <GenerateIdeasModal
        open={generateOpen}
        onClose={() => setGenerateOpen(false)}
        pid={pid}
        runId={run?.id ?? null}
        direction={direction}
        libraryId={libraryId}
        paperCount={paperCount}
        onStarted={() => void voyagesQuery.refetch()}
        onOpenProposal={() => setProposalOpen(true)}
      />
      <CompareModal open={compareOpen} onClose={() => setCompareOpen(false)} pid={pid} ideas={all} onStarted={() => void voyagesQuery.refetch()} />
      <DeepDiveDrawer open={proposalOpen} onClose={() => setProposalOpen(false)} pid={pid} initialSeedIdea={null} />
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
    </div>
  );
}
