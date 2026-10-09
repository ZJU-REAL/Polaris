import { useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { StatusPill } from '../../components/ui/StatusPill';
import { Segmented } from '../../components/ui/Segmented';
import { Modal } from '../../components/ui/Modal';
import { KnobRange } from '../../components/ui/KnobRange';
import { FormField } from '../../components/ui/FormField';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { topicPath, useProject } from '../../app/project';
import { fmtTime } from '../../lib/format';
import { tr } from '../../lib/i18n';
import {
  api,
  ApiError,
  type LeaderboardRow,
  type ReviewPersona,
  type ReviewSessionRead,
} from '../../lib/api';
import { MiniScoreBars } from '../forge/ideaShared';
import { classifyDebateAuthors, DebateBubble, DiscussionBubble } from './messages';

/* ============================================================
   /review — Stage 02 · Idea Review（M3）
   Tab ① 排行榜：GET leaderboard + 运行锦标赛 Modal；
   Tab ② 辩论记录：选 idea → sessions(idea_match) → 逐轮气泡。
   深链：?tab=matches&idea=<id>（idea 详情页跳入）。
   ============================================================ */

type ReviewTab = 'leaderboard' | 'matches';

/** 默认评审人设（渲染时求值，随语言切换）。 */
function defaultPersonas(): ReviewPersona[] {
  return [
    { name: tr('方法论审稿人', 'Methods reviewer'), stance: tr('找方法和实验设计的漏洞', 'Looks for flaws in method and experiment design') },
    { name: tr('前沿视角审稿人', 'Frontier reviewer'), stance: tr('看新颖性和潜在影响', 'Judges novelty and potential impact') },
    { name: tr('工程审稿人', 'Engineering reviewer'), stance: tr('看可行性和实现成本', 'Judges feasibility and cost to build') },
  ];
}

/* ---------------- 运行锦标赛 Modal ---------------- */

function TournamentModal({ open, onClose, pid }: { open: boolean; onClose: () => void; pid: string }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [rounds, setRounds] = useState(2);
  const [personas, setPersonas] = useState<ReviewPersona[]>(defaultPersonas);

  const mutation = useMutation({
    mutationFn: () =>
      api.startTournament(pid, {
        idea_ids: null,
        rounds,
        personas: personas.filter((p) => p.name.trim() !== ''),
      }),
    onSuccess: (v) => {
      toast(tr('已开始评审', 'Review started'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['forge-state', pid] });
      onClose();
      navigate(`/voyages/${v.id}`);
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) {
        toast(
          tr('已有想法生成或评审任务在运行，请等它完成', 'An idea generation or review task is already running. Wait for it to finish.'),
          'error',
        );
        void queryClient.invalidateQueries({ queryKey: ['forge-state', pid] });
      } else {
        toast(`${tr('无法开始评审：', 'Couldn’t start the review: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
      }
    },
  });

  function setPersona(i: number, patch: Partial<ReviewPersona>) {
    setPersonas((ps) => ps.map((p, pi) => (pi === i ? { ...p, ...patch } : p)));
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      width={560}
      title={
        <>
          <Icon name="scale" size={16} style={{ color: 'var(--accent)' }} />
          {tr('评审想法', 'Review ideas')}
        </>
      }
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>{tr('取消', 'Cancel')}</button>
          <button className="btn btn-primary" disabled={mutation.isPending} onClick={() => mutation.mutate()}>
            {mutation.isPending ? (
              <>
                <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
                {tr('启动中…', 'Starting…')}
              </>
            ) : (
              <>
                <Icon name="play" size={14} />
                {tr('开始评审', 'Start review')}
              </>
            )}
          </button>
        </>
      }
    >
      <KnobRange
        label="辩论轮数"
        en="Debate rounds"
        hint={tr('每两个想法之间辩论几轮', 'How many rounds each pair of ideas debates')}
        value={rounds}
        min={1}
        max={5}
        step={1}
        onChange={setRounds}
      />
      <FormField
        label={tr('AI 审稿人', 'AI reviewers')}
        hint={tr('每行一位审稿人，可修改或增删', 'One reviewer per row. Edit, add or remove.')}
      >
        <div className="col gap8">
          {personas.map((p, i) => (
            <div key={i} className="row gap8">
              <input
                className="input"
                style={{ width: 150, flexShrink: 0 }}
                placeholder={tr('名字', 'Name')}
                value={p.name}
                onChange={(e) => setPersona(i, { name: e.target.value })}
              />
              <input
                className="input"
                style={{ flex: 1 }}
                placeholder={tr('关注点', 'Focus')}
                value={p.stance}
                onChange={(e) => setPersona(i, { stance: e.target.value })}
              />
              <button
                className="icon-btn"
                title={tr('移除', 'Remove')}
                onClick={() => setPersonas((ps) => ps.filter((_, pi) => pi !== i))}
              >
                <Icon name="trash" size={14} />
              </button>
            </div>
          ))}
          <button
            className="btn btn-soft sm"
            style={{ alignSelf: 'flex-start' }}
            onClick={() => setPersonas((ps) => [...ps, { name: '', stance: '' }])}
          >
            <Icon name="plus" size={13} />
            {tr('添加审稿人', 'Add reviewer')}
          </button>
        </div>
      </FormField>
      <div style={{ fontSize: 11, color: 'var(--text-3)', lineHeight: 1.6 }}>
        {tr(
          '所有候选和评审中的想法两两辩论，你在讨论区的评论也会交给审稿人参考。',
          'All candidate and in-review ideas debate in pairs. Reviewers also see your comments in the discussion.',
        )}
      </div>
    </Modal>
  );
}

/* ---------------- Tab ① 排行榜 ---------------- */

// 表头与各行是独立 grid，列宽必须定宽才能上下对齐；状态列要放得下
// 「评审中 under review」pill，操作列要放得下「图标 + 发起实验」，
// 否则右对齐的操作区会向左溢出、盖住状态标签
const LB_GRID = '36px minmax(220px,1fr) 64px 150px 48px 48px 150px 136px';
// 标题列给 220px 下限、其余定宽列合计 632px、7 个 12px gap、左右 padding 36：
// 合计 972。窄屏低于此宽度就整块横滚，而不是让标题列被挤成 0 宽整列消失。
const LB_MIN_W = 972;

function LeaderboardTab({
  pid,
  rows,
  loading,
  error,
  refetch,
  running,
  onOpenMatches,
}: {
  pid: string;
  rows: LeaderboardRow[];
  loading: boolean;
  error: boolean;
  refetch: () => void;
  running: boolean;
  onOpenMatches: (ideaId: string) => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const promoteMutation = useMutation({
    mutationFn: (ideaId: string) => api.promoteIdea(ideaId),
    onSuccess: () => {
      toast(tr('已提交晋级审批', 'Sent for promotion approval'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['gates'] });
      void queryClient.invalidateQueries({ queryKey: ['leaderboard', pid] });
      void queryClient.invalidateQueries({ queryKey: ['ideas'] });
    },
    onError: (e) => toast(`${tr('无法晋级：', 'Couldn’t promote: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  if (loading) return <div className="empty" style={{ padding: 40 }}>{tr('加载中…', 'Loading…')}</div>;
  if (error) {
    return (
      <EmptyState
        compact
        icon="x"
        title={tr('无法加载排行榜', 'Couldn’t load the leaderboard')}
        desc={tr('请确认本机引擎正在运行，然后重试。', 'Make sure the local engine is running, then retry.')}
        action={<button className="btn btn-soft sm" onClick={refetch}>{tr('重试', 'Retry')}</button>}
      />
    );
  }
  if (rows.length === 0) {
    return (
      <EmptyState
        icon="chart"
        title={tr('还没有评审过的想法', 'No reviewed ideas yet')}
        desc={tr('先生成想法，再开始评审。', 'Generate some ideas, then run a review.')}
        action={
          <button className="btn btn-ghost" onClick={() => navigate(topicPath(pid, 'forge'))}>
            <Icon name="bulb" size={14} />
            {tr('去生成想法', 'Generate ideas')}
          </button>
        }
      />
    );
  }

  return (
    <div className="table-wrap">
      <div style={{ minWidth: LB_MIN_W }}>
        {/* 表头 */}
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: LB_GRID,
            gap: 12,
            padding: '10px 18px',
            borderBottom: '0.5px solid var(--border)',
            fontSize: 11,
            fontWeight: 600,
            color: 'var(--text-3)',
            textTransform: 'uppercase',
            letterSpacing: '0.04em',
          }}
        >
          <span>#</span>
          <span>{tr('想法', 'Idea')}</span>
          <span style={{ textAlign: 'right' }}>Elo</span>
          <span>{tr('四项评分', 'Rubric scores')}</span>
          <span style={{ textAlign: 'right' }}>{tr('场次', 'Matches')}</span>
          <span style={{ textAlign: 'right' }}>{tr('胜', 'Wins')}</span>
          <span>{tr('状态', 'Status')}</span>
          <span />
        </div>
        {rows.map((r, i) => {
          const promotable = (r.status === 'candidate' || r.status === 'under_review');
          return (
            <div
              key={r.id}
              className="hoverable"
              onClick={() => navigate(`/ideas/${r.id}`)}
              style={{
                display: 'grid',
                gridTemplateColumns: LB_GRID,
                gap: 12,
                alignItems: 'center',
                padding: '12px 18px',
                borderBottom: '0.5px solid var(--border)',
              }}
            >
              <span
                className="mono"
                style={{ fontSize: 13, fontWeight: 600, color: i < 3 ? 'var(--accent-text)' : 'var(--text-3)' }}
              >
                {i + 1}
              </span>
              <div style={{ minWidth: 0 }}>
                <div
                  style={{
                    fontSize: 13,
                    fontWeight: 600,
                    whiteSpace: 'nowrap',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                  }}
                >
                  {r.title}
                </div>
                <div
                  style={{
                    fontSize: 11,
                    color: 'var(--text-3)',
                    marginTop: 2,
                    whiteSpace: 'nowrap',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                  }}
                >
                  {r.summary}
                </div>
              </div>
              <span className="mono" style={{ fontSize: 15, fontWeight: 600, color: 'var(--accent-text)', textAlign: 'right' }}>
                {Math.round(r.elo_rating)}
              </span>
              <MiniScoreBars scores={r.scores} />
              <span className="mono" style={{ fontSize: 13, textAlign: 'right' }}>{r.matches}</span>
              <span className="mono" style={{ fontSize: 13, textAlign: 'right', color: 'var(--ok-tx)' }}>{r.wins}</span>
              <StatusPill status={r.status} sm />
              <div className="row gap6" style={{ justifyContent: 'flex-end' }} onClick={(e) => e.stopPropagation()}>
                <button
                  className="icon-btn"
                  title={tr('辩论记录', 'Debate history')}
                  onClick={() => onOpenMatches(r.id)}
                  style={{ width: 26, height: 26 }}
                >
                  <Icon name="scale" size={13} />
                </button>
                {promotable && (
                  <button
                    className="btn btn-soft sm"
                    disabled={running || promoteMutation.isPending}
                    onClick={() => promoteMutation.mutate(r.id)}
                  >
                    {tr('晋级', 'Promote')}
                  </button>
                )}
                {r.status === 'promoted' && (
                  <button
                    className="btn btn-soft sm"
                    title={tr('用这个想法新建实验', 'Start an experiment from this idea')}
                    onClick={() => navigate(topicPath(pid, `experiment?new=${r.id}`))}
                  >
                    <Icon name="flask" size={12} />
                    {tr('发起实验', 'Start experiment')}
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/* ---------------- Tab ② 辩论记录 ---------------- */

function matchMeta(s: ReviewSessionRead, ideaId: string, titleOf: (id: string) => string) {
  const p = s.payload ?? {};
  const a = typeof p.idea_a === 'string' ? p.idea_a : null;
  const b = typeof p.idea_b === 'string' ? p.idea_b : null;
  const winnerRaw = typeof p.winner === 'string' ? p.winner : null;
  const winnerId = winnerRaw === 'a' ? a : winnerRaw === 'b' ? b : winnerRaw;
  const oppId = a === ideaId ? b : a;
  const won = winnerId !== null && winnerId === ideaId;
  const decided = winnerId !== null && winnerId !== undefined;
  return {
    oppTitle: oppId ? titleOf(oppId) : tr('未知想法', 'Unknown idea'),
    won,
    decided,
  };
}

function MatchesTab({
  ideaId,
  onSelectIdea,
  rows,
  rowsError,
}: {
  ideaId: string | null;
  onSelectIdea: (id: string) => void;
  rows: LeaderboardRow[];
  rowsError: boolean;
}) {
  const [selectedSession, setSelectedSession] = useState<string | null>(null);

  const titleOf = (id: string) => rows.find((r) => r.id === id)?.title ?? tr('已删除的想法', 'Deleted idea');

  const sessionsQuery = useQuery({
    queryKey: ['idea-sessions', ideaId],
    queryFn: () => api.listIdeaSessions(ideaId!),
    enabled: !!ideaId,
    retry: false,
  });
  const matches = (sessionsQuery.data ?? []).filter((s) => s.target_type === 'idea_match');
  const activeSession =
    matches.find((s) => s.id === selectedSession) ?? (matches.length > 0 ? matches[0] : undefined);

  const messagesQuery = useQuery({
    queryKey: ['session-messages', activeSession?.id ?? null],
    queryFn: () => api.listSessionMessages(activeSession!.id),
    enabled: !!activeSession,
    retry: false,
  });
  const messages = messagesQuery.data ?? [];
  const roles = useMemo(() => classifyDebateAuthors(messages), [messages]);

  return (
    <div style={{ padding: '16px 18px' }}>
      {/* idea 选择 */}
      <div className="row gap10" style={{ marginBottom: 16 }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-2)', flexShrink: 0 }}>
          {tr('想法', 'Idea')}
        </span>
        <select
          className="input"
          style={{ flex: '0 1 480px', minWidth: 0 }}
          value={ideaId ?? ''}
          onChange={(e) => {
            setSelectedSession(null);
            onSelectIdea(e.target.value);
          }}
        >
          <option value="" disabled>
            {rowsError ? tr('无法加载想法', 'Couldn’t load ideas') : tr('选择想法', 'Choose an idea')}
          </option>
          {rows.map((r) => (
            <option key={r.id} value={r.id}>
              {tr(
                `${r.title}（Elo ${Math.round(r.elo_rating)} · ${r.matches} 场）`,
                `${r.title} (Elo ${Math.round(r.elo_rating)} · ${r.matches} ${r.matches === 1 ? 'match' : 'matches'})`,
              )}
            </option>
          ))}
        </select>
      </div>

      {!ideaId ? (
        <EmptyState
          compact
          icon="scale"
          title={tr('选择一个想法查看它的辩论', 'Choose an idea to see its debates')}
        />
      ) : sessionsQuery.isLoading ? (
        <div className="empty" style={{ padding: 30 }}>{tr('加载中…', 'Loading…')}</div>
      ) : sessionsQuery.isError ? (
        <EmptyState
          compact
          icon="x"
          title={tr('无法加载辩论', 'Couldn’t load debates')}
          desc={tr('请确认本机引擎正在运行。', 'Make sure the local engine is running.')}
        />
      ) : matches.length === 0 ? (
        <EmptyState
          compact
          icon="scale"
          title={tr('这个想法还没参加过辩论', 'This idea hasn’t debated yet')}
        />
      ) : (
        <div className="row gap16" style={{ alignItems: 'flex-start', flexWrap: 'wrap' }}>
          {/* 场次列表（窄屏时换到记录上方） */}
          <div className="col gap8" style={{ flex: '1 1 260px', maxWidth: 320, minWidth: 0 }}>
            {matches.map((s) => {
              const meta = matchMeta(s, ideaId, titleOf);
              const active = s.id === activeSession?.id;
              return (
                <div
                  key={s.id}
                  className="hoverable"
                  onClick={() => setSelectedSession(s.id)}
                  style={{
                    border: `0.5px solid ${active ? 'var(--accent-soft-2)' : 'var(--border)'}`,
                    borderLeft: `2px solid ${active ? 'var(--accent)' : 'transparent'}`,
                    borderRadius: 10,
                    padding: '10px 13px',
                    background: active ? 'var(--accent-soft)' : 'var(--surface)',
                  }}
                >
                  <div className="row gap8" style={{ marginBottom: 5 }}>
                    {meta.decided ? (
                      <span className="row gap6" style={{ fontSize: 12, color: 'var(--text-2)' }}>
                        <span
                          style={{
                            width: 8,
                            height: 8,
                            borderRadius: '50%',
                            background: meta.won ? 'var(--ok)' : 'var(--danger)',
                          }}
                        />
                        {meta.won ? tr('胜', 'Won') : tr('负', 'Lost')}
                      </span>
                    ) : (
                      <StatusPill status={s.status} sm />
                    )}
                    <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)', marginLeft: 'auto' }}>
                      {fmtTime(s.created_at)}
                    </span>
                  </div>
                  <div style={{ fontSize: 12, lineHeight: 1.4 }}>
                    <span style={{ color: 'var(--text-3)' }}>vs </span>
                    <b>{meta.oppTitle}</b>
                  </div>
                </div>
              );
            })}
          </div>

          {/* 逐轮记录 */}
          <div style={{ flex: '999 1 360px', minWidth: 0 }}>
            {!activeSession ? (
              <EmptyState compact icon="scale" title={tr('选择一场辩论', 'Choose a debate')} />
            ) : messagesQuery.isLoading ? (
              <div className="empty" style={{ padding: 30 }}>{tr('加载中…', 'Loading…')}</div>
            ) : messagesQuery.isError ? (
              <EmptyState compact icon="x" title={tr('无法加载辩论内容', 'Couldn’t load this debate')} />
            ) : messages.length === 0 ? (
              <EmptyState compact icon="scale" title={tr('这场辩论还没有发言', 'No messages in this debate yet')} />
            ) : (
              <div>
                {messages.map((m) =>
                  m.author_type === 'human' ? (
                    <DiscussionBubble key={m.id} msg={m} />
                  ) : (
                    <DebateBubble key={m.id} msg={m} role={roles.get(m.author_name) ?? 'other'} />
                  ),
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/* ---------------- 页面 ---------------- */

export function ReviewPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const { isLoading: projectsLoading, currentProject, currentProjectId } = useProject();
  const pid = currentProjectId;

  const tab: ReviewTab = searchParams.get('tab') === 'matches' ? 'matches' : 'leaderboard';
  const focusIdea = searchParams.get('idea');
  const [modalOpen, setModalOpen] = useState(false);

  function setTab(t: ReviewTab) {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (t === 'matches') next.set('tab', 'matches');
        else next.delete('tab');
        return next;
      },
      { replace: true },
    );
  }
  function setFocusIdea(id: string) {
    setSearchParams({ tab: 'matches', idea: id }, { replace: true });
  }

  // 与 forge 共用 running 状态（同项目同时只允许一个 forge/review voyage）
  const stateQuery = useQuery({
    queryKey: ['forge-state', pid],
    queryFn: () => api.getForgeState(pid!),
    enabled: !!pid,
    retry: false,
    refetchInterval: (q) => (q.state.data?.running_voyage_id ? 5_000 : 60_000),
  });
  const runningVoyage = stateQuery.data?.running_voyage_id ?? null;

  const leaderboardQuery = useQuery({
    queryKey: ['leaderboard', pid],
    queryFn: () => api.getLeaderboard(pid!),
    enabled: !!pid,
    retry: false,
  });
  const rows = leaderboardQuery.data ?? [];

  const tournamentQuery = useQuery({
    queryKey: ['latest-tournament', pid],
    queryFn: () => api.getLatestTournamentSummary(pid!),
    enabled: !!pid,
    retry: false,
    refetchInterval: runningVoyage ? 5_000 : false,
  });
  const latestTournament = tournamentQuery.data ?? null;


  const retryMutation = useMutation({
    mutationFn: () => api.retryFailedTournamentMatches(pid!),
    onSuccess: (voyage) => {
      toast(tr('已开始重试', 'Retry started'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['latest-tournament', pid] });
      void queryClient.invalidateQueries({ queryKey: ['forge-state', pid] });
      navigate(`/voyages/${voyage.id}`);
    },
    onError: (e) =>
      toast(
        `${tr('无法重试：', 'Couldn’t retry: ')}${e instanceof Error ? e.message : String(e)}`,
        'error',
      ),
  });

  return (
    <div className="page fadeup" style={{ maxWidth: 1280 }}>
      <PageHead
        eyebrow="Stage 02 · Idea Review"
        title={tr('想法评审', 'Idea Review')}
        sub={
          currentProject
            ? undefined
            : projectsLoading
              ? tr('加载中…', 'Loading…')
              : tr('请先选择课题', 'Choose a topic first')
        }
      />

      {runningVoyage && (
        <div
          className="card card-pad hoverable"
          onClick={() => navigate(`/voyages/${runningVoyage}`)}
          style={{ marginBottom: 16, borderColor: 'var(--accent-soft-2)', background: 'var(--accent-soft)' }}
        >
          <div className="row gap10">
            <span className="pulse" style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--ok)', flexShrink: 0 }} />
            <span style={{ fontSize: 13, fontWeight: 600 }}>
              {tr('想法生成或评审正在进行', 'Idea generation or review in progress')}
            </span>
            <span style={{ fontSize: 12, color: 'var(--accent-text)', marginLeft: 'auto' }}>
              {tr('查看进度', 'View progress')}
            </span>
            <Icon name="arrow" size={14} style={{ color: 'var(--accent-text)' }} />
          </div>
        </div>
      )}

      {!runningVoyage && latestTournament && latestTournament.failed > 0 && (
        <div
          className="card card-pad"
          style={{ marginBottom: 16, borderColor: 'var(--warn)', background: 'var(--warn-bg)' }}
        >
          <div className="row gap10">
            <Icon name="x" size={15} style={{ color: 'var(--warn)' }} />
            <span style={{ fontSize: 13, fontWeight: 600 }}>
              {tr(
                `上次评审有 ${latestTournament.failed} 场辩论没有完成（共 ${latestTournament.planned} 场）`,
                `${latestTournament.failed} of ${latestTournament.planned} debates in the last review didn’t finish`,
              )}
            </span>
            {latestTournament.can_retry && (
              <button
                className="btn btn-soft sm"
                style={{ marginLeft: 'auto' }}
                disabled={retryMutation.isPending}
                onClick={() => retryMutation.mutate()}
              >
                <Icon name="refresh" size={13} />
                {tr(
                  '重试未完成的辩论',
                  'Retry unfinished debates',
                )}
              </button>
            )}
          </div>
        </div>
      )}

      <div className="row page-tabs" style={{ marginBottom: 14, justifyContent: 'space-between' }}>
        <Segmented<ReviewTab>
          options={[
            { v: 'leaderboard', label: `${tr('排行榜', 'Leaderboard')}${rows.length ? ` · ${rows.length}` : ''}` },
            { v: 'matches', label: tr('辩论记录', 'Debates') },
          ]}
          value={tab}
          onChange={setTab}
        />
          <button className="btn btn-primary sm" disabled={!pid || !!runningVoyage} onClick={() => setModalOpen(true)}>
            {runningVoyage ? (
              <>
                <Icon name="refresh" size={13} style={{ animation: 'spin 1s linear infinite' }} />
                {tr('运行中…', 'Running…')}
              </>
            ) : (
              <>
                <Icon name="play" size={13} />
                {tr('开始评审', 'Run review')}
              </>
            )}
          </button>
      </div>

      <div className="card" style={{ overflow: 'hidden', minHeight: 320 }}>
        {!pid ? (
          <div className="empty" style={{ padding: 60 }}>
            {projectsLoading ? tr('加载中…', 'Loading…') : tr('请先选择课题', 'Choose a topic first')}
          </div>
        ) : tab === 'leaderboard' ? (
          <LeaderboardTab
            pid={pid}
            rows={rows}
            loading={leaderboardQuery.isLoading}
            error={leaderboardQuery.isError}
            refetch={() => void leaderboardQuery.refetch()}
            running={!!runningVoyage}
            onOpenMatches={setFocusIdea}
          />
        ) : (
          <MatchesTab
            ideaId={focusIdea}
            onSelectIdea={setFocusIdea}
            rows={rows}
            rowsError={leaderboardQuery.isError}
          />
        )}
      </div>

      {pid && <TournamentModal open={modalOpen} onClose={() => setModalOpen(false)} pid={pid} />}
    </div>
  );
}
