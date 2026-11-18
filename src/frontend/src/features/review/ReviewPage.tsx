import { useMemo, useState } from 'react';
import { Link, Navigate, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { EmptyState, ErrorState, LoadingState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { topicPath, useProject } from '../../app/project';
import { errorText } from '../../lib/errors';
import { tr } from '../../lib/i18n';
import { api, type LeaderboardRow } from '../../lib/api';
import { CompareModal, comparePlan } from '../forge/CompareModal';
import { overallScore, rankIdeas, rankText, recordText } from '../forge/ideaView';
import '../voyages/task.css';
import '../forge/ideas.css';
import './review.css';

/* ============================================================
   /t/:id/review —— 想法对比的结果：一张名次表。

   名次平时就写在「想法」页的卡片上；这一页是完整的那张表（谁第几、比了几场、
   赢了几场、综合评分），从想法页的「对比记录」和「更多」进来。每一场的讨论在各个
   想法自己的详情里——点一行就到。旧链接 `?tab=matches&idea=` 直接转到那个想法。
   ============================================================ */

const IN_FLIGHT = new Set(['planning', 'executing', 'verifying', 'replanning']);

export function ReviewPage() {
  const queryClient = useQueryClient();
  const [searchParams] = useSearchParams();
  const { currentProjectId: pid } = useProject();
  const [compareOpen, setCompareOpen] = useState(false);

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
    refetchInterval: (q) => ((q.state.data ?? []).some((v) => v.kind === 'idea_review' && IN_FLIGHT.has(v.status)) ? 4_000 : 30_000),
  });
  const running = (voyagesQuery.data ?? []).find((v) => ['idea_forge', 'idea_review', 'idea_proposal'].includes(v.kind) && IN_FLIGHT.has(v.status)) ?? null;
  const comparing = running?.kind === 'idea_review';
  const tournamentQuery = useQuery({
    queryKey: ['latest-tournament', pid],
    queryFn: () => api.getLatestTournamentSummary(pid!),
    enabled: !!pid,
    retry: false,
    refetchInterval: comparing ? 4_000 : false,
  });
  const tournament = tournamentQuery.data ?? null;

  const retry = useMutation({
    mutationFn: () => api.retryFailedTournamentMatches(pid!),
    onSuccess: () => {
      toast(tr('已开始重试', 'Retry started'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['latest-tournament', pid] });
      void queryClient.invalidateQueries({ queryKey: ['voyages'] });
    },
    onError: (e) => toast(errorText(e), 'error'),
  });
  const pick = useMutation({
    mutationFn: (ideaId: string) => api.pickIdea(ideaId),
    onSuccess: () => {
      toast(tr('已选定', 'Picked'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['leaderboard', pid] });
      void queryClient.invalidateQueries({ queryKey: ['ideas'] });
      void queryClient.invalidateQueries({ queryKey: ['idea'] });
      void queryClient.invalidateQueries({ queryKey: ['research'] });
    },
    onError: (e) => toast(errorText(e), 'error'),
  });

  const rows = leaderboardQuery.data ?? [];
  const ranks = useMemo(() => rankIdeas(rows), [rows]);
  const ranked: LeaderboardRow[] = useMemo(
    () => rows.filter((r) => ranks.has(r.id)).sort((a, b) => ranks.get(a.id)!.rank - ranks.get(b.id)!.rank || b.wins - a.wins || (overallScore(b.scores) ?? 0) - (overallScore(a.scores) ?? 0)),
    [rows, ranks],
  );
  const unranked = rows.filter((r) => !ranks.has(r.id) && r.status !== 'rejected');
  const plan = comparePlan(rows);

  // 旧深链：某个想法的辩论记录现在在它的详情里
  const focus = searchParams.get('idea');
  if (focus) return <Navigate to={`/ideas/${focus}#comparisons`} replace />;
  if (!pid) return null;

  return (
    <div className="page fadeup">
      <div className="ideas-body">
        <div className="ideas-head">
          <div className="ideas-head-text">
            <h2 className="ideas-lead">{tr('想法对比', 'Idea comparisons')}</h2>
            <span className="ideas-note">
              {tr('AI 审稿人把想法两两对比，按胜负排出名次。每一场的讨论在各个想法的详情里。', 'AI reviewers compare ideas in pairs and rank them by the results. Each discussion is in the idea’s own page.')}
            </span>
          </div>
          <div className="ideas-head-actions">
            <Link className="btn btn-plain sm" to={topicPath(pid, 'forge')} style={{ textDecoration: 'none' }}>
              {tr('全部想法', 'All ideas')}
            </Link>
            {plan.matches > 0 && (
              <button
                type="button"
                className="btn btn-primary sm"
                disabled={!!running}
                title={running ? tr('等正在运行的想法任务完成', 'Wait for the running idea task to finish') : undefined}
                onClick={() => setCompareOpen(true)}
              >
                {ranked.length > 0 ? tr('重新对比', 'Compare again') : tr('开始对比', 'Start comparing')}
              </button>
            )}
          </div>
        </div>

        {comparing && running && (
          <div className="ideas-status" style={{ marginTop: 16, marginBottom: 0 }} data-testid="compare-running">
            <span className="task-dot is-active" />
            <span className="ideas-status-text">
              {tournament && tournament.planned > 0
                ? tr(`正在对比，已完成 ${tournament.completed}/${tournament.planned} 场`, `Comparing: ${tournament.completed} of ${tournament.planned} done`)
                : tr('正在对比排序', 'Comparing ideas')}
            </span>
            <Link className="btn btn-plain xs" to={`/voyages/${running.id}`}>
              {tr('查看进度', 'View progress')}
            </Link>
          </div>
        )}
        {!comparing && tournament && tournament.failed > 0 && (
          <div className="ideas-compare is-warn">
            <span>{tr(`上次对比有 ${tournament.failed} 场没完成（共 ${tournament.planned} 场）`, `${tournament.failed} of ${tournament.planned} comparisons didn’t finish last time`)}</span>
            {tournament.can_retry && (
              <button type="button" className="btn btn-ghost xs" disabled={retry.isPending || !!running} onClick={() => retry.mutate()}>
                {tr('重试没完成的', 'Retry unfinished')}
              </button>
            )}
          </div>
        )}

        {leaderboardQuery.isLoading ? (
          <div style={{ marginTop: 16 }}>
            <LoadingState rows={4} />
          </div>
        ) : leaderboardQuery.isError ? (
          <ErrorState title={tr('无法加载名次，请重试', 'Couldn’t load the ranking. Try again.')} detail={errorText(leaderboardQuery.error)} onRetry={() => void leaderboardQuery.refetch()} />
        ) : ranked.length === 0 ? (
          <EmptyState
            icon="scale"
            title={tr('还没有对比过', 'Nothing compared yet')}
            desc={
              plan.matches > 0
                ? tr('对比后，这里列出每个想法的名次和胜负。', 'After a comparison, each idea’s rank and results are listed here.')
                : tr('至少要有两个想法才能对比。', 'You need at least two ideas to compare.')
            }
            action={
              plan.matches > 0 ? undefined : (
                <Link className="btn btn-ghost" to={topicPath(pid, 'forge')} style={{ textDecoration: 'none' }}>
                  {tr('去想法页', 'Go to Ideas')}
                </Link>
              )
            }
          />
        ) : (
          <>
            <ol className="rank-list" data-testid="rank-list">
              {ranked.map((r) => {
                const rank = ranks.get(r.id)!;
                const overall = overallScore(r.scores);
                return (
                  <li key={r.id} className="rank-row" data-idea={r.id}>
                    <span className="rank-n">{rankText(rank)}</span>
                    <Link className="rank-main" to={`/ideas/${r.id}#comparisons`}>
                      <span className="rank-title" title={r.title}>
                        {r.title}
                      </span>
                      <span className="rank-sub">
                        {recordText(rank)}
                        {overall !== null ? tr(` · 综合评分 ${overall.toFixed(1)}`, ` · Overall score ${overall.toFixed(1)}`) : ''}
                      </span>
                    </Link>
                    <span className="rank-act">
                      {r.status === 'promoted' ? (
                        <span className="rank-state">{tr('已选定', 'Picked')}</span>
                      ) : r.status === 'rejected' ? (
                        <span className="rank-state is-quiet">{tr('已淘汰', 'Rejected')}</span>
                      ) : (
                        <button type="button" className="btn btn-ghost sm" disabled={pick.isPending} onClick={() => pick.mutate(r.id)}>
                          {tr('选定', 'Pick')}
                        </button>
                      )}
                    </span>
                  </li>
                );
              })}
            </ol>
            {unranked.length > 0 && (
              <div className="ideas-compare">
                {tr(`另有 ${unranked.length} 个想法还没对比过。`, `${unranked.length} more ${unranked.length === 1 ? 'idea hasn’t' : 'ideas haven’t'} been compared yet.`)}
              </div>
            )}
          </>
        )}
      </div>
      <CompareModal open={compareOpen} onClose={() => setCompareOpen(false)} pid={pid} ideas={rows} onStarted={() => void voyagesQuery.refetch()} />
    </div>
  );
}
