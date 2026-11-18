import { useCallback, useMemo, useState, type ReactNode } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { DotsMenu, type DotsMenuItem } from '../../components/ui/DotsMenu';
import { ErrorState, LoadingState } from '../../components/ui/EmptyState';
import { Timeline, TimelineItem } from '../../components/ui/Timeline';
import { toast } from '../../components/ui/Toast';
import { Markdown } from '../../lib/markdown';
import { fmtTime } from '../../lib/format';
import {
  api,
  type IdeaDetail,
  type IdeaEvidence,
  type IdeaGoal,
  type ReviewMessageRead,
  type ReviewSessionRead,
} from '../../lib/api';
import { errorText } from '../../lib/errors';
import { tr } from '../../lib/i18n';
import { topicPath, useProject, useSyncTopic } from '../../app/project';
import { DiscussionPanel } from '../review/DiscussionPanel';
import { DiscussionBubble } from '../review/messages';
import { researchKeys } from '../deepresearch/hooks';
import { IdeaComparisons, matchInfo } from './IdeaComparisons';
import { researchTypeLabel, SCORE_DIMS } from './ideaShared';
import { splitInlineEnumerations } from './ideaText';
import {
  groupEvidence,
  localizeHeadings,
  originKind,
  originSentence,
  overallScore,
  rankIdeas,
  standingText,
  splitUnsupportedSection,
  unsupportedOf,
} from './ideaView';
import '../voyages/task.css';
import './ideas.css';

/* ============================================================
   /ideas/:id —— 一个想法，从上往下读：

     它主张什么（标题 + 一段话）→ 现在能对它做什么（选定 / 开始实验 / 展开为研究方案）
     → 为什么可能成立（文献依据，按支持 / 反对分组，点论文就能读）
     → 哪些地方还没有依据 → 打算怎么做（方法与实验）→ 评分 → 和别的想法比的结果 → 讨论。

   以前是两栏卡片：正文在左，评分条、依据、来源论文、操作挤在右栏，
   辩论记录要跳去另一页。
   ============================================================ */

/** goal JSON 由模型写，列表字段可能是单个字符串——统一成列表。 */
function asList(v: unknown): string[] {
  if (Array.isArray(v)) return v.map((x) => (typeof x === 'string' ? x : JSON.stringify(x)));
  if (typeof v === 'string' && v.trim()) return [v];
  return [];
}

function Section({ title, note, aside, children, id }: { title: string; note?: ReactNode; aside?: ReactNode; children: ReactNode; id?: string }) {
  return (
    <section className="idea-sec" id={id} data-sec={id}>
      <div className="idea-sec-head">
        <h2 className="idea-sec-title">{title}</h2>
        {note && <span className="idea-sec-note">{note}</span>}
        {aside && <span className="idea-sec-aside">{aside}</span>}
      </div>
      {children}
    </section>
  );
}

/* ---------------- 文献依据 ---------------- */

function EvidenceItem({ ev }: { ev: IdeaEvidence }) {
  const [open, setOpen] = useState(false);
  const snippet = typeof ev.snippet === 'string' ? ev.snippet.trim() : '';
  return (
    <li className="idea-ev-item">
      {ev.why && <div className="idea-ev-why">{ev.why}</div>}
      <div className="idea-ev-src">
        {ev.paper_id ? (
          <Link to={`/papers/${ev.paper_id}/read`} className="idea-ev-paper" title={ev.title}>
            {ev.title}
          </Link>
        ) : ev.url ? (
          <a href={ev.url} target="_blank" rel="noreferrer noopener" className="idea-ev-paper" title={ev.title}>
            {ev.title}
          </a>
        ) : (
          <span className="idea-ev-paper" title={ev.title}>
            {ev.title}
          </span>
        )}
        {!ev.paper_id && <span className="idea-ev-tag">{ev.source === 'external' ? tr('不在文献库里', 'Not in your library') : tr('趋势信号', 'Trend signal')}</span>}
        {snippet && (
          <button type="button" className="step-toggle idea-ev-tag" style={{ marginTop: 0 }} aria-expanded={open} onClick={() => setOpen((o) => !o)}>
            {open ? tr('收起原文', 'Hide excerpt') : tr('原文片段', 'Excerpt')}
          </button>
        )}
      </div>
      {open && snippet && <blockquote className="idea-ev-snippet">{snippet}</blockquote>}
    </li>
  );
}

function EvidenceSection({ idea }: { idea: IdeaDetail }) {
  const grouped = groupEvidence(idea.evidence);
  // 趋势信号不是论文对论断的表态（没有立场）：单独一组，不算「支持」
  const isLead = (e: IdeaEvidence) => e.source === 'signal' && !e.stance;
  const support = grouped.support.filter((e) => !isLead(e));
  const leads = grouped.support.filter(isLead);
  const refute = grouped.refute;
  const [allParents, setAllParents] = useState(false);
  const cited = new Set((idea.evidence ?? []).map((e) => e.paper_id).filter(Boolean));
  const parents = idea.parent_papers.filter((p) => !cited.has(p.id));
  const shownParents = allParents ? parents : parents.slice(0, 5);
  const stanced = support.length + refute.length;
  const total = stanced + leads.length;

  const group = (label: string, tone: string, list: IdeaEvidence[]) =>
    list.length > 0 && (
      <div className="idea-ev-group">
        <div className="idea-ev-label">
          <span className={'task-dot ' + tone} />
          {label}
          <span className="idea-sec-note">{list.length}</span>
        </div>
        <ul className="idea-ev-list">
          {list.map((ev, i) => (
            <EvidenceItem key={`${ev.paper_id ?? ev.title}-${i}`} ev={ev} />
          ))}
        </ul>
      </div>
    );

  return (
    <Section id="evidence" title={tr('文献依据', 'Evidence')} note={stanced > 0 ? tr('每条是一句论断和支持或反对它的论文。', 'Each line is a claim and the paper that supports or contradicts it.') : undefined}>
      {group(tr('支持', 'Supporting'), 'is-ok', support)}
      {group(tr('反对', 'Against'), 'is-err', refute)}
      {group(tr('提出它的线索', 'What prompted it'), '', leads)}
      {total === 0 && parents.length === 0 && <div className="idea-empty">{tr('这个想法没有记录文献依据。', 'No evidence from the papers is recorded for this idea.')}</div>}
      {parents.length > 0 && (
        <div className="idea-ev-group">
          <div className="idea-ev-label">
            {total > 0 ? tr('还参考了', 'Also drew on') : tr('提出时参考的论文', 'Papers it was drawn from')}
            <span className="idea-sec-note">{parents.length}</span>
          </div>
          {total === 0 && (
            <div className="idea-sec-note" style={{ marginBottom: 6 }}>
              {tr('快速生成的想法只记录参考了哪些论文，没有逐条核对论断。', 'Quick ideas only record which papers they drew on; the claims weren’t checked one by one.')}
            </div>
          )}
          <ul className="idea-plain-list">
            {shownParents.map((p) => (
              <li key={p.id}>
                <Link to={`/papers/${p.id}/read`}>{p.title}</Link>
              </li>
            ))}
          </ul>
          {parents.length > 5 && (
            <button type="button" className="step-toggle" onClick={() => setAllParents((v) => !v)}>
              {allParents ? tr('收起', 'Show fewer') : tr(`显示全部 ${parents.length} 篇`, `Show all ${parents.length}`)}
            </button>
          )}
        </div>
      )}
    </Section>
  );
}

/* ---------------- 研究目标（研究方案才有） ---------------- */

function GoalSection({ goal }: { goal: IdeaGoal }) {
  const [showSmoke, setShowSmoke] = useState(false);
  const res = goal.resources_needed;
  const scopeObj = goal.scope && typeof goal.scope === 'object' ? goal.scope : undefined;
  const scopeText = typeof goal.scope === 'string' ? goal.scope : null;
  const inScope = asList(scopeObj?.in_scope);
  const outScope = asList(scopeObj?.out_of_scope);
  const objectives = asList(goal.objectives);
  const success = asList(goal.success_criteria);
  const concepts = asList(goal.key_concepts);
  const data = asList(res?.data);
  const resources = [
    res?.compute ? tr(`算力：${res.compute}`, `Compute: ${res.compute}`) : null,
    data.length > 0 ? tr(`数据：${data.join('、')}`, `Data: ${data.join(', ')}`) : null,
    res?.time_weeks != null ? tr(`约 ${res.time_weeks} 周`, `About ${res.time_weeks} weeks`) : null,
  ].filter((x): x is string => !!x);
  const row = (label: string, body: ReactNode) => (
    <>
      <dt>{label}</dt>
      <dd>{body}</dd>
    </>
  );
  const list = (items: string[]) => (
    <ul>
      {items.map((s, i) => (
        <li key={i}>{s}</li>
      ))}
    </ul>
  );
  return (
    <Section id="goal" title={tr('研究目标', 'Research goal')} note={goal.research_type ? researchTypeLabel(goal.research_type) : undefined}>
      <dl className="idea-goal">
        {goal.question && row(tr('核心问题', 'Core question'), goal.question)}
        {goal.task && row(tr('研究任务', 'Task'), goal.task)}
        {objectives.length > 0 && row(tr('具体目标', 'Objectives'), list(objectives))}
        {scopeText && row(tr('研究范围', 'Scope'), scopeText)}
        {(inScope.length > 0 || outScope.length > 0) &&
          row(
            tr('研究范围', 'Scope'),
            <>
              {inScope.length > 0 && (
                <>
                  <div className="idea-goal-sub">{tr('做什么', 'In scope')}</div>
                  {list(inScope)}
                </>
              )}
              {outScope.length > 0 && (
                <>
                  <div className="idea-goal-sub">{tr('不做什么', 'Out of scope')}</div>
                  {list(outScope)}
                </>
              )}
            </>,
          )}
        {success.length > 0 && row(tr('成功标准', 'Success criteria'), list(success))}
        {concepts.length > 0 && row(tr('关键概念', 'Key concepts'), concepts.join(tr('、', ', ')))}
        {resources.length > 0 && row(tr('资源需求', 'Resources'), resources.join(tr('；', '; ')))}
      </dl>
      {goal.smoke_plan && Object.keys(goal.smoke_plan).length > 0 && (
        <>
          <button type="button" className="step-toggle" aria-expanded={showSmoke} onClick={() => setShowSmoke((v) => !v)}>
            {showSmoke ? tr('收起快速验证实验', 'Hide the quick validation plan') : tr('快速验证实验（1–3 天见结果）', 'Quick validation plan (results in 1–3 days)')}
          </button>
          {showSmoke && (
            <pre className="codeblock" style={{ fontSize: 11, marginTop: 8 }}>
              {JSON.stringify(goal.smoke_plan, null, 2)}
            </pre>
          )}
        </>
      )}
    </Section>
  );
}

/* ---------------- 写研究方案时的评审与修订（只读） ---------------- */

function roundInfoOf(payload: Record<string, unknown> | null, round: number): { scores: string | null; summary: string | null } {
  const empty = { scores: null, summary: null };
  const roundsRaw = payload?.rounds;
  if (!Array.isArray(roundsRaw)) return empty;
  const item = roundsRaw.find((r) => r && typeof r === 'object' && Number((r as Record<string, unknown>).round) === round) as Record<string, unknown> | undefined;
  if (!item) return empty;
  const summaryRaw = item.revision_summary ?? item.summary;
  const summary = typeof summaryRaw === 'string' && summaryRaw.trim() !== '' ? summaryRaw : null;
  const scoresRec = item.scores && typeof item.scores === 'object' ? (item.scores as Record<string, unknown>) : null;
  const scores = scoresRec
    ? SCORE_DIMS.filter((d) => typeof scoresRec[d.key] === 'number')
        .map((d) => `${tr(d.zh, d.en)} ${(scoresRec[d.key] as number).toFixed(1)}`)
        .join(' · ') || null
    : null;
  return { scores, summary };
}

function RevisionSessionBlock({ session }: { session: ReviewSessionRead }) {
  const messagesQuery = useQuery({
    queryKey: ['session-messages', session.id],
    queryFn: () => api.listSessionMessages(session.id),
    retry: false,
  });
  const messages = messagesQuery.data ?? [];
  const rounds = useMemo(() => {
    const map = new Map<number, ReviewMessageRead[]>();
    for (const m of messages) {
      const r = m.round ?? 0;
      if (!map.has(r)) map.set(r, []);
      map.get(r)!.push(m);
    }
    return [...map.entries()].sort((a, b) => a[0] - b[0]);
  }, [messages]);

  if (messagesQuery.isLoading) return <LoadingState compact rows={2} />;
  if (rounds.length === 0) return <div className="idea-empty">{tr('还没有评审意见', 'No review comments yet')}</div>;
  return (
    <Timeline>
      {rounds.map(([round, msgs], i) => {
        const info = roundInfoOf(session.payload, round);
        return (
          <TimelineItem key={round} marker={String(round > 0 ? round : i + 1)} markerBg="var(--surface-3)" markerColor="var(--text-2)" last={i === rounds.length - 1}>
            <div className="row gap8" style={{ marginBottom: 8, flexWrap: 'wrap' }}>
              <span style={{ fontSize: 13, fontWeight: 600 }}>{tr(`第 ${round > 0 ? round : i + 1} 轮评审`, `Review round ${round > 0 ? round : i + 1}`)}</span>
              {info.scores && <span style={{ fontSize: 12, color: 'var(--text-3)' }}>{info.scores}</span>}
            </div>
            {msgs.map((m) => (
              <DiscussionBubble key={m.id} msg={m} />
            ))}
            {info.summary && (
              <div style={{ fontSize: 12, color: 'var(--text-2)', lineHeight: 1.55 }}>
                <b style={{ color: 'var(--text)', fontWeight: 500 }}>{tr('这一轮改了什么：', 'What changed: ')}</b>
                {info.summary}
              </div>
            )}
          </TimelineItem>
        );
      })}
    </Timeline>
  );
}

/* ---------------- 页面 ---------------- */

export function IdeaDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const { currentProjectId, projects } = useProject();
  const [confirmTrash, setConfirmTrash] = useState(false);
  const [showRevisions, setShowRevisions] = useState(false);

  const ideaQuery = useQuery({
    queryKey: ['idea', id],
    queryFn: () => api.getIdea(id!),
    enabled: !!id,
    retry: false,
  });
  const idea = ideaQuery.data;
  const pid = idea?.project_id ?? null;
  useSyncTopic(pid);

  const leaderboardQuery = useQuery({
    queryKey: ['leaderboard', pid],
    queryFn: () => api.getLeaderboard(pid!),
    enabled: !!pid,
    retry: false,
  });
  const sessionsQuery = useQuery({
    queryKey: ['idea-sessions', id],
    queryFn: () => api.listIdeaSessions(id!),
    enabled: !!id,
    retry: false,
  });
  const voyagesQuery = useQuery({
    queryKey: ['voyages', pid],
    queryFn: () => api.listVoyages(pid!),
    enabled: !!pid,
    retry: false,
  });

  const rank = useMemo(() => (id ? rankIdeas(leaderboardQuery.data ?? []).get(id) : undefined), [leaderboardQuery.data, id]);
  const titles = useMemo(() => new Map((leaderboardQuery.data ?? []).map((r) => [r.id, r.title])), [leaderboardQuery.data]);
  const matches = useMemo(
    () => (sessionsQuery.data ?? []).filter((s) => s.target_type === 'idea_match').map((s) => matchInfo(s, id ?? '')),
    [sessionsQuery.data, id],
  );
  const revisions = (sessionsQuery.data ?? []).filter((s) => s.target_type === 'idea_revision');
  const ideaTaskRunning = (voyagesQuery.data ?? []).some(
    (v) => ['idea_forge', 'idea_review', 'idea_proposal'].includes(v.kind) && ['planning', 'executing', 'verifying', 'replanning'].includes(v.status),
  );

  function invalidate() {
    void queryClient.invalidateQueries({ queryKey: ['idea', id] });
    void queryClient.invalidateQueries({ queryKey: ['ideas'] });
    void queryClient.invalidateQueries({ queryKey: ['leaderboard', pid] });
    void queryClient.invalidateQueries({ queryKey: ['forge-state'] });
    void queryClient.invalidateQueries({ queryKey: researchKeys.list });
    void queryClient.invalidateQueries({ queryKey: ['research'] });
  }

  // 选定 / 取消选定 / 淘汰 / 恢复：都是直接改状态，可以反悔
  const statusMutation = useMutation({
    mutationFn: (status: 'candidate' | 'promoted' | 'rejected') => (status === 'promoted' ? api.pickIdea(id!) : api.patchIdea(id!, { status })),
    onSuccess: (updated, requested) => {
      const wasRejected = idea?.status === 'rejected';
      toast(
        requested === 'promoted' ? tr('已选定', 'Picked') : requested === 'rejected' ? tr('已淘汰', 'Rejected') : wasRejected ? tr('已恢复', 'Restored') : tr('已取消选定', 'Unpicked'),
        'ok',
      );
      queryClient.setQueryData<IdeaDetail>(['idea', id], (prev) => (prev ? { ...prev, status: updated.status } : prev));
      invalidate();
    },
    onError: (e) => toast(errorText(e), 'error'),
  });
  const expand = useMutation({
    mutationFn: () => api.startDeepIdea(pid!, { seed: { type: 'idea', value: id! } }),
    onSuccess: (voyage) => {
      toast(tr('已开始写研究方案', 'Started writing the proposal'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['voyages'] });
      navigate(`/voyages/${voyage.id}`);
    },
    onError: (e) => toast(errorText(e), 'error'),
  });
  const trash = useMutation({
    mutationFn: () => api.trashIdea(id!),
    onSuccess: () => {
      toast(tr('已移入回收站', 'Moved to trash'), 'ok');
      invalidate();
      navigate(topicPath(pid, 'forge'));
    },
    onError: (e) => toast(errorText(e), 'error'),
    onSettled: () => setConfirmTrash(false),
  });

  // —— [[paper:uuid]] → 论文链接（标题取自依据 / 参考论文） ——
  const paperTitles = useMemo(() => {
    const map = new Map<string, string>();
    for (const p of idea?.parent_papers ?? []) map.set(p.id, p.title);
    for (const ev of idea?.evidence ?? []) if (ev.paper_id) map.set(ev.paper_id, ev.title);
    return map;
  }, [idea]);
  const renderPaperRef = useCallback(
    (paperId: string) => (
      <Link className="wikilink" to={`/papers/${paperId}/read`} title={tr('打开这篇论文', 'Open this paper')}>
        {paperTitles.get(paperId) ?? tr('文献库里的论文', 'Paper in your library')}
      </Link>
    ),
    [paperTitles],
  );

  const body = useMemo(() => {
    if (!idea?.content) return { text: '', unsupported: [] as string[] };
    const split = splitUnsupportedSection(idea.content);
    return { text: splitInlineEnumerations(localizeHeadings(split.body)), unsupported: split.unsupported };
  }, [idea?.content]);

  if (ideaQuery.isLoading) {
    return (
      <div className="page fadeup idea-page">
        <LoadingState rows={5} />
      </div>
    );
  }
  if (ideaQuery.isError || !idea) {
    return (
      <div className="page fadeup idea-page">
        <ErrorState
          title={tr('无法加载这个想法，它可能已被删除', 'Couldn’t load this idea. It may have been deleted.')}
          detail={ideaQuery.error ? errorText(ideaQuery.error) : null}
          onRetry={() => void ideaQuery.refetch()}
          action={
            <Link className="btn btn-ghost sm" to={topicPath(currentProjectId, 'forge')}>
              {tr('返回想法', 'Back to ideas')}
            </Link>
          }
        />
      </div>
    );
  }

  const picked = idea.status === 'promoted';
  const rejected = idea.status === 'rejected';
  const proposal = idea.depth === 'proposal';
  const origin = originKind(idea);
  const fromList = unsupportedOf(idea);
  const unsupported = fromList.length > 0 ? fromList : body.unsupported;
  const overall = overallScore(idea.scores);
  const topicName = projects.find((p) => p.id === idea.project_id)?.name ?? '';
  const busy = statusMutation.isPending;
  const startExperiment = topicPath(idea.project_id, `experiment?new=${idea.id}`);
  const expandTitle = ideaTaskRunning ? tr('等正在运行的想法任务完成', 'Wait for the running idea task to finish') : undefined;

  const menu: DotsMenuItem[] = [
    ...(picked ? [{ key: 'unpick', label: tr('取消选定', 'Unpick'), onSelect: () => statusMutation.mutate('candidate') } as DotsMenuItem] : []),
    ...(!rejected ? [{ key: 'reject', label: tr('淘汰', 'Reject'), icon: 'x', onSelect: () => statusMutation.mutate('rejected') } as DotsMenuItem] : []),
    { key: 'trash', label: tr('移入回收站', 'Move to trash'), icon: 'trash', danger: true, separated: true, onSelect: () => setConfirmTrash(true) },
  ];

  return (
    <div className="page fadeup idea-page">
      <Link className="task-back" to={topicPath(idea.project_id, 'forge')}>
        ← {topicName ? tr(`${topicName} · 想法`, `${topicName} · Ideas`) : tr('想法', 'Ideas')}
      </Link>
      <h1 className="task-title" title={idea.title}>
        {idea.title}
      </h1>
      {idea.summary && idea.summary.trim() !== idea.title.trim() && <p className="idea-summary">{idea.summary}</p>}

      <div className="idea-meta">
        {(picked || rejected) && (
          <span className="idea-state" data-idea-state={idea.status}>
            <span className={'task-dot ' + (picked ? 'is-ok' : '')} />
            {picked ? tr('已选定', 'Picked') : tr('已淘汰', 'Rejected')}
          </span>
        )}
        {rank && (
          <a href="#comparisons" className="is-strong" data-testid="idea-rank">
            {standingText(rank)}
          </a>
        )}
        {origin !== 'manual' && <span>{originSentence(origin)}</span>}
        {idea.seed_idea && (
          <span>
            {tr('由想法展开：', 'Expanded from: ')}
            <Link to={`/ideas/${idea.seed_idea.id}`}>{idea.seed_idea.title}</Link>
          </span>
        )}
        <span>{fmtTime(idea.created_at)}</span>
      </div>

      <div className="idea-actions" data-idea-actions={idea.status}>
        {rejected ? (
          <button type="button" className="btn btn-ghost" disabled={busy} onClick={() => statusMutation.mutate('candidate')}>
            {tr('恢复', 'Restore')}
          </button>
        ) : picked ? (
          <Link className="btn btn-primary" to={startExperiment}>
            {tr('开始实验', 'Start experiment')}
          </Link>
        ) : (
          <button type="button" className="btn btn-primary" disabled={busy} onClick={() => statusMutation.mutate('promoted')}>
            {tr('选定', 'Pick')}
          </button>
        )}
        {!rejected && !proposal && (
          <button type="button" className="btn btn-ghost" disabled={expand.isPending || ideaTaskRunning} title={expandTitle} onClick={() => expand.mutate()}>
            {expand.isPending ? tr('正在开始…', 'Starting…') : tr('展开为研究方案', 'Expand into a proposal')}
          </button>
        )}
        {!rejected && !picked && (
          <Link className="btn btn-ghost" to={startExperiment}>
            {tr('开始实验', 'Start experiment')}
          </Link>
        )}
        <DotsMenu items={menu} align="left" />
        <span className="idea-actions-hint">
          {rejected
            ? tr('已淘汰的想法不参加对比，也不会出现在实验和写作的选项里。', 'Rejected ideas are left out of comparisons and aren’t offered for experiments or writing.')
            : picked
              ? tr('已选定：它会排在实验和写作的选项最前面。', 'Picked: it is offered first for experiments and writing.')
              : tr('选定你想继续做的想法；可以随时取消。', 'Pick the ideas you want to pursue; you can unpick at any time.')}
        </span>
      </div>

      <EvidenceSection idea={idea} />

      {unsupported.length > 0 && (
        <Section id="unsupported" title={tr('尚无依据', 'Not yet supported')} note={tr('这些论断在文献库里没找到依据，要靠实验来检验。', 'No paper in your library backs these claims; an experiment has to test them.')}>
          <ul className="idea-plain-list">
            {unsupported.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ul>
        </Section>
      )}

      {idea.goal && <GoalSection goal={idea.goal} />}

      <Section id="plan" title={proposal ? tr('研究方案', 'Proposal') : tr('方法与实验', 'Method and experiments')}>
        {body.text ? (
          <div className="idea-body">
            <Markdown source={body.text} renderPaperRef={renderPaperRef} />
          </div>
        ) : (
          <div className="idea-empty">{tr('还没有写方法和实验。', 'No method or experiments written yet.')}</div>
        )}
        {revisions.length > 0 && (
          <>
            <button type="button" className="step-toggle" aria-expanded={showRevisions} onClick={() => setShowRevisions((v) => !v)}>
              {showRevisions ? tr('收起评审与修订', 'Hide reviews and revisions') : tr('这份方案的评审与修订', 'Reviews and revisions of this proposal')}
            </button>
            {showRevisions && (
              <div style={{ marginTop: 12 }}>
                {revisions.map((s) => (
                  <RevisionSessionBlock key={s.id} session={s} />
                ))}
              </div>
            )}
          </>
        )}
      </Section>

      <Section
        id="scores"
        title={tr('评分', 'Scores')}
        note={
          idea.scores
            ? [
                overall !== null ? tr(`综合 ${overall.toFixed(1)}，满分 10。`, `Overall ${overall.toFixed(1)} out of 10.`) : '',
                idea.origin?.novelty_scope === 'library' ? tr('新颖性只对照你的文献库判断。', 'Novelty is judged against your library only.') : '',
              ]
                .filter(Boolean)
                .join(' ')
            : undefined
        }
      >
        {idea.scores ? (
          <dl className="idea-scores">
            {SCORE_DIMS.map((d) => (
              <div key={d.key} style={{ display: 'contents' }}>
                <dt>{tr(d.zh, d.en)}</dt>
                <dd className="idea-score-n">{Number.isInteger(idea.scores![d.key]) ? idea.scores![d.key] : idea.scores![d.key].toFixed(1)}</dd>
                <dd className="idea-score-why">{idea.score_rationale?.[d.key] ?? ''}</dd>
              </div>
            ))}
          </dl>
        ) : (
          <div className="idea-empty">{tr('还没有评分。', 'Not scored yet.')}</div>
        )}
      </Section>

      <Section
        id="comparisons"
        title={tr('对比记录', 'Comparisons')}
        note={matches.length > 0 ? tr('点开一场看审稿人的讨论。', 'Open one to read the reviewers’ discussion.') : undefined}
      >
        {sessionsQuery.isLoading ? (
          <LoadingState compact rows={2} />
        ) : matches.length === 0 ? (
          <div className="idea-empty">
            {tr('还没和别的想法对比过。在想法页点「对比排序」，AI 审稿人会把想法两两对比、排出名次。', 'Not compared with other ideas yet. Use Compare on the Ideas page: AI reviewers compare ideas in pairs and rank them.')}
          </div>
        ) : (
          <IdeaComparisons
            matches={matches}
            titleOf={(oid) => (oid ? (titles.get(oid) ?? tr('已删除的想法', 'Deleted idea')) : tr('未知想法', 'Unknown idea'))}
            initialOpen={searchParams.get('match')}
          />
        )}
      </Section>

      <Section id="discussion" title={tr('讨论', 'Discussion')} note={tr('下次对比时，AI 审稿人会看到这里的评论。', 'AI reviewers read these comments in the next comparison.')}>
        <DiscussionPanel ideaId={idea.id} />
      </Section>

      <ConfirmModal
        open={confirmTrash}
        onClose={() => setConfirmTrash(false)}
        title={tr(`把「${idea.title}」移入回收站？`, `Move “${idea.title}” to the trash?`)}
        message={tr('之后可以在想法页的回收站里恢复。', 'You can restore it from the trash on the Ideas page.')}
        confirmText={tr('移入回收站', 'Move to trash')}
        danger
        busy={trash.isPending}
        onConfirm={() => trash.mutate()}
      />
    </div>
  );
}
