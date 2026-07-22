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
import { useProject } from '../../app/project';
import { fmtTime } from '../../lib/format';
import { tr } from '../../lib/i18n';
import {
  api,
  ApiError,
  isAdmin,
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

const DEFAULT_PERSONAS: ReviewPersona[] = [
  { name: '严谨方法论者', stance: '专挑方法与实验设计漏洞' },
  { name: '前沿趋势派', stance: '关注新颖性与领域影响力上限' },
  { name: '务实工程师', stance: '审视可行性与实现成本' },
];

/* ---------------- 运行锦标赛 Modal ---------------- */

function TournamentModal({ open, onClose, pid }: { open: boolean; onClose: () => void; pid: string }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [rounds, setRounds] = useState(2);
  const [personas, setPersonas] = useState<ReviewPersona[]>(DEFAULT_PERSONAS);

  const mutation = useMutation({
    mutationFn: () =>
      api.startTournament(pid, {
        idea_ids: null,
        rounds,
        personas: personas.filter((p) => p.name.trim() !== ''),
      }),
    onSuccess: (v) => {
      toast(tr('评审锦标赛已开始，跳转任务详情…', 'Review tournament started — opening task detail…'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['forge-state', pid] });
      onClose();
      navigate(`/voyages/${v.id}`);
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) {
        toast(
          tr('该项目已有一个 forge/review 任务在运行，请等待其完成。', 'This project already has a forge/review task running — wait for it to finish.'),
          'error',
        );
        void queryClient.invalidateQueries({ queryKey: ['forge-state', pid] });
      } else {
        toast(`${tr('启动失败', 'Failed to start')}：${e instanceof Error ? e.message : String(e)}`, 'error');
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
          {tr('运行评审锦标赛', 'Run review tournament')}
        </>
      }
      sub={tr('候选想法两两配对辩论，按胜负更新排名。', 'Candidate ideas debate in pairs; rankings update by wins and losses.')}
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
                {tr('开始锦标赛', 'Start tournament')}
              </>
            )}
          </button>
        </>
      }
    >
      <KnobRange
        label={tr('辩论轮数', 'Debate rounds')}
        en="rounds"
        hint={tr('每对想法的正反辩论轮数。', 'Rounds of pro/con debate per pair of ideas.')}
        value={rounds}
        min={1}
        max={5}
        step={1}
        onChange={setRounds}
      />
      <FormField
        label={tr('评审人设', 'Reviewer personas')}
        en="personas"
        hint={tr(
          '每个人设是一个 agent 评审：名字 + 立场（stance）。默认三人设可直接编辑。',
          'Each persona is one agent reviewer: a name plus a stance. The three defaults are editable.',
        )}
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
                placeholder={tr('立场', 'Stance')}
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
            {tr('添加人设', 'Add persona')}
          </button>
        </div>
      </FormField>
      <div style={{ fontSize: 11, color: 'var(--text-4)', lineHeight: 1.6 }}>
        {tr(
          '对全部候选与评审中的想法进行配对辩论；讨论区中的人工评论会提供给相关 AI 评审员参考。',
          'All candidate and under-review ideas debate in pairs; human comments from the discussion are shared with the relevant AI reviewers.',
        )}
      </div>
    </Modal>
  );
}

/* ---------------- Tab ① 排行榜 ---------------- */

// 表头与各行是独立 grid，列宽必须定宽才能上下对齐；状态列要放得下
// 「评审中 under review」pill，操作列要放得下「图标 + 发起实验」，
// 否则右对齐的操作区会向左溢出、盖住状态标签
const LB_GRID = '36px minmax(0,1fr) 64px 150px 48px 48px 150px 136px';

function LeaderboardTab({
  pid,
  rows,
  loading,
  error,
  refetch,
  canPromote,
  running,
  onOpenMatches,
}: {
  pid: string;
  rows: LeaderboardRow[];
  loading: boolean;
  error: boolean;
  refetch: () => void;
  canPromote: boolean;
  running: boolean;
  onOpenMatches: (ideaId: string) => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const promoteMutation = useMutation({
    mutationFn: (ideaId: string) => api.promoteIdea(ideaId),
    onSuccess: () => {
      toast(tr('已提交晋级审批，等待人工审批', 'Promotion approval created — awaiting human approval'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['gates'] });
      void queryClient.invalidateQueries({ queryKey: ['leaderboard', pid] });
      void queryClient.invalidateQueries({ queryKey: ['ideas'] });
    },
    onError: (e) => toast(`${tr('晋级失败', 'Promotion failed')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  if (loading) return <div className="empty" style={{ padding: 40 }}>{tr('加载排行榜…', 'Loading leaderboard…')}</div>;
  if (error) {
    return (
      <EmptyState
        compact
        icon="x"
        title={tr('无法加载排行榜', 'Failed to load leaderboard')}
        desc={tr('后端不可用或接口尚未就绪。', 'Backend unavailable or the API is not ready yet.')}
        action={<button className="btn btn-soft sm" onClick={refetch}>{tr('重试', 'Retry')}</button>}
      />
    );
  }
  if (rows.length === 0) {
    return (
      <EmptyState
        icon="chart"
        title={tr('排行榜为空', 'Leaderboard is empty')}
        desc={tr('先在想法生成页生成候选想法，再运行一次评审。', 'Generate candidate ideas on the Idea Forge page first, then run a review.')}
        action={
          <button className="btn btn-ghost" onClick={() => navigate('/forge')}>
            <Icon name="bulb" size={14} />
            {tr('前往想法生成', 'Go to Idea Forge')}
          </button>
        }
      />
    );
  }

  return (
    <div>
      {/* 表头 */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: LB_GRID,
          gap: 12,
          padding: '10px 18px',
          borderBottom: '0.5px solid var(--border)',
          fontSize: 10.5,
          fontWeight: 650,
          color: 'var(--text-3)',
          textTransform: 'uppercase',
          letterSpacing: '0.04em',
        }}
      >
        <span>#</span>
        <span>{tr('想法', 'Idea')}</span>
        <span style={{ textAlign: 'right' }}>Elo</span>
        <span>{tr('四维 rubric', 'Rubric (4 dims)')}</span>
        <span style={{ textAlign: 'right' }}>{tr('对局', 'Matches')}</span>
        <span style={{ textAlign: 'right' }}>{tr('胜场', 'Wins')}</span>
        <span>{tr('状态', 'Status')}</span>
        <span />
      </div>
      {rows.map((r, i) => {
        const promotable = canPromote && (r.status === 'candidate' || r.status === 'under_review');
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
              style={{ fontSize: 14, fontWeight: 700, color: i < 3 ? 'var(--accent-text)' : 'var(--text-3)' }}
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
            <span className="mono" style={{ fontSize: 14.5, fontWeight: 700, color: 'var(--accent-text)', textAlign: 'right' }}>
              {Math.round(r.elo_rating)}
            </span>
            <MiniScoreBars scores={r.scores} />
            <span className="mono" style={{ fontSize: 12.5, textAlign: 'right' }}>{r.matches}</span>
            <span className="mono" style={{ fontSize: 12.5, textAlign: 'right', color: 'var(--ok-tx)' }}>{r.wins}</span>
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
                  className="btn btn-primary sm"
                  disabled={running || promoteMutation.isPending}
                  onClick={() => promoteMutation.mutate(r.id)}
                >
                  {tr('晋级', 'Promote')}
                </button>
              )}
              {r.status === 'promoted' && (
                <button
                  className="btn btn-soft sm"
                  title={tr('从该想法发起实验', 'Start an experiment from this idea')}
                  onClick={() => navigate(`/experiment?new=${r.id}`)}
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
    oppTitle: oppId ? titleOf(oppId) : tr('未知对手', 'Unknown opponent'),
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

  const titleOf = (id: string) => rows.find((r) => r.id === id)?.title ?? `${id.slice(0, 8)}…`;

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
        <span style={{ fontSize: 12.5, fontWeight: 600, color: 'var(--text-2)', flexShrink: 0 }}>
          {tr('选择想法', 'Pick an idea')}
        </span>
        <select
          className="input"
          style={{ maxWidth: 480 }}
          value={ideaId ?? ''}
          onChange={(e) => {
            setSelectedSession(null);
            onSelectIdea(e.target.value);
          }}
        >
          <option value="" disabled>
            {rowsError ? tr('（排行榜不可用）', '(leaderboard unavailable)') : tr('— 从排行榜选择 —', '— pick from the leaderboard —')}
          </option>
          {rows.map((r) => (
            <option key={r.id} value={r.id}>
              {tr(
                `${r.title}（Elo ${Math.round(r.elo_rating)} · ${r.matches} 场）`,
                `${r.title} (Elo ${Math.round(r.elo_rating)} · ${r.matches} matches)`,
              )}
            </option>
          ))}
        </select>
      </div>

      {!ideaId ? (
        <EmptyState
          compact
          icon="scale"
          title={tr('选择一个想法', 'Pick an idea')}
          desc={tr('从上方下拉或排行榜点入，查看其全部辩论场次。', 'Pick from the dropdown above or click through from the leaderboard to see all its debates.')}
        />
      ) : sessionsQuery.isLoading ? (
        <div className="empty" style={{ padding: 30 }}>{tr('加载场次…', 'Loading matches…')}</div>
      ) : sessionsQuery.isError ? (
        <EmptyState
          compact
          icon="x"
          title={tr('无法加载辩论场次', 'Failed to load debate matches')}
          desc={tr('后端不可用或接口尚未就绪。', 'Backend unavailable or the API is not ready yet.')}
        />
      ) : matches.length === 0 ? (
        <EmptyState
          compact
          icon="scale"
          title={tr('暂无对局记录', 'No matches yet')}
          desc={tr('运行一次评审后，这里会出现该想法的每场辩论。', 'Run a review and every debate for this idea will show up here.')}
        />
      ) : (
        <div className="row gap16" style={{ alignItems: 'flex-start' }}>
          {/* 场次列表 */}
          <div className="col gap8" style={{ width: 300, flexShrink: 0 }}>
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
                      <span
                        className="pill sm"
                        style={{
                          background: meta.won ? 'var(--ok-bg)' : 'var(--danger-bg)',
                          color: meta.won ? 'var(--ok-tx)' : 'var(--danger-tx)',
                        }}
                      >
                        {meta.won ? 'WIN' : 'LOSS'}
                      </span>
                    ) : (
                      <StatusPill status={s.status} sm />
                    )}
                    <span className="mono" style={{ fontSize: 10, color: 'var(--text-4)', marginLeft: 'auto' }}>
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
          <div style={{ flex: 1, minWidth: 0 }}>
            {!activeSession ? (
              <EmptyState compact icon="scale" title={tr('选择一场辩论', 'Pick a debate')} />
            ) : messagesQuery.isLoading ? (
              <div className="empty" style={{ padding: 30 }}>{tr('加载辩论记录…', 'Loading debate transcript…')}</div>
            ) : messagesQuery.isError ? (
              <EmptyState compact icon="x" title={tr('无法加载辩论记录', 'Failed to load debate transcript')} />
            ) : messages.length === 0 ? (
              <EmptyState compact icon="scale" title={tr('该场辩论暂无发言', 'No messages in this debate yet')} />
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

  // 晋级按钮 owner 可见：项目 owner 或平台 admin；成员信息缺失时放行（后端仍会校验）
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: () => api.me(), retry: false, staleTime: 60_000 });
  const members = currentProject?.members;
  const canPromote =
    isAdmin(me) ||
    !members ||
    members.some((m) => m.role === 'owner' && ((me?.id && m.user_id === me.id) || (me?.email && m.email === me.email)));

  return (
    <div className="page fadeup" style={{ maxWidth: 1280 }}>
      <PageHead
        eyebrow="Stage 02 · Idea Review"
        title={tr('想法评审', 'Idea Review')}
        sub={
          currentProject
            ? `${tr('当前课题', 'Current topic')}：${currentProject.name}`
            : projectsLoading
              ? tr('加载课题…', 'Loading topics…')
              : tr('选择一个课题', 'Pick a topic')
        }
        right={
          <button className="btn btn-primary" disabled={!pid || !!runningVoyage} onClick={() => setModalOpen(true)}>
            {runningVoyage ? (
              <>
                <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
                {tr('运行中…', 'Running…')}
              </>
            ) : (
              <>
                <Icon name="play" size={14} />
                {tr('运行锦标赛', 'Run tournament')}
              </>
            )}
          </button>
        }
      />

      {runningVoyage && (
        <div
          className="card card-pad hoverable"
          onClick={() => navigate(`/voyages/${runningVoyage}`)}
          style={{ marginBottom: 16, borderColor: 'var(--accent-soft-2)', background: 'var(--accent-soft)' }}
        >
          <div className="row gap10">
            <span className="pill" style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>
              <span className="dot pulse" />
              {tr('运行中', 'Running')}
            </span>
            <span style={{ fontSize: 13.5, fontWeight: 650 }}>
              {tr('forge/review 任务进行中 — 点击查看任务实时进度', 'A forge/review task is running — click to watch live progress')}
            </span>
            <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)', marginLeft: 'auto' }}>
              voyage {runningVoyage.slice(0, 8)}…
            </span>
            <Icon name="arrow" size={14} style={{ color: 'var(--accent-text)' }} />
          </div>
        </div>
      )}

      <div className="row" style={{ marginBottom: 14, justifyContent: 'space-between' }}>
        <Segmented<ReviewTab>
          options={[
            { v: 'leaderboard', label: `${tr('排行榜', 'Leaderboard')}${rows.length ? ` · ${rows.length}` : ''}` },
            { v: 'matches', label: tr('辩论记录', 'Debates') },
          ]}
          value={tab}
          onChange={setTab}
        />
      </div>

      <div className="card" style={{ overflow: 'hidden', minHeight: 320 }}>
        {!pid ? (
          <div className="empty" style={{ padding: 60 }}>
            {projectsLoading ? tr('加载课题…', 'Loading topics…') : tr('请先选择课题', 'Pick a topic first')}
          </div>
        ) : tab === 'leaderboard' ? (
          <LeaderboardTab
            pid={pid}
            rows={rows}
            loading={leaderboardQuery.isLoading}
            error={leaderboardQuery.isError}
            refetch={() => void leaderboardQuery.refetch()}
            canPromote={canPromote}
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
