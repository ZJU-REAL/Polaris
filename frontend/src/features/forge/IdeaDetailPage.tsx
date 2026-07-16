import { useCallback, useMemo, useState, type ReactNode } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { StatusPill } from '../../components/ui/StatusPill';
import { EmptyState } from '../../components/ui/EmptyState';
import { Timeline, TimelineItem } from '../../components/ui/Timeline';
import { toast } from '../../components/ui/Toast';
import { Markdown } from '../../lib/markdown';
import { fmtTime } from '../../lib/format';
import {
  api,
  type IdeaDetail,
  type IdeaEvidenceSource,
  type IdeaGoal,
  type ReviewMessageRead,
  type ReviewSessionRead,
} from '../../lib/api';
import { useShell } from '../../app/AppShell';
import { DiscussionPanel } from '../review/DiscussionPanel';
import { DiscussionBubble } from '../review/messages';
import { compositeOf, DepthBadge, ResearchTypeBadge, RubricBar, SCORE_DIMS } from './ideaShared';

/* ============================================================
   /ideas/:id — idea 详情（M3 + Idea 2.0）
   content markdown（[[paper:uuid]] → 库内论文链接）、四维分数 +
   rationale 折叠、parent papers 链接（跳 wiki）、晋级 / 人工淘汰、
   研究方案：研究目标卡片 + 依据文献 + 评审修订记录（只读），
   底部人机讨论区。
   ============================================================ */

/* ---------------- 研究目标卡片（Idea 2.0） ---------------- */

function GoalField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ fontSize: 10.5, fontWeight: 700, color: 'var(--text-3)', marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 12.5, lineHeight: 1.6 }}>{children}</div>
    </div>
  );
}

function GoalCard({ goal }: { goal: IdeaGoal }) {
  const [showSmoke, setShowSmoke] = useState(false);
  const res = goal.resources_needed;
  const inScope = goal.scope?.in_scope ?? [];
  const outScope = goal.scope?.out_of_scope ?? [];
  return (
    <div className="card card-pad">
      <div className="row gap8" style={{ marginBottom: 14 }}>
        <span className="section-h">
          <Icon name="compass" size={14} style={{ color: 'var(--accent)' }} />
          研究目标 <span className="en-label" style={{ fontSize: 11 }}>Research Goal</span>
        </span>
        <span style={{ marginLeft: 'auto' }}>
          <ResearchTypeBadge type={goal.research_type} />
        </span>
      </div>
      {goal.task && <GoalField label="研究任务">{goal.task}</GoalField>}
      {goal.question && (
        <GoalField label="核心问题">
          <b>{goal.question}</b>
        </GoalField>
      )}
      {(goal.objectives?.length ?? 0) > 0 && (
        <GoalField label="研究目标">
          <ol style={{ margin: 0, paddingLeft: 18 }}>
            {goal.objectives!.map((o, i) => (
              <li key={i} style={{ marginBottom: 3 }}>{o}</li>
            ))}
          </ol>
        </GoalField>
      )}
      {(inScope.length > 0 || outScope.length > 0) && (
        <GoalField label="研究范围">
          <div className="row gap12 wrap" style={{ alignItems: 'flex-start' }}>
            {inScope.length > 0 && (
              <div style={{ flex: 1, minWidth: 180 }}>
                <div style={{ fontSize: 11, fontWeight: 650, color: 'var(--ok-tx)', marginBottom: 3 }}>做什么</div>
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {inScope.map((s, i) => (
                    <li key={i}>{s}</li>
                  ))}
                </ul>
              </div>
            )}
            {outScope.length > 0 && (
              <div style={{ flex: 1, minWidth: 180 }}>
                <div style={{ fontSize: 11, fontWeight: 650, color: 'var(--text-3)', marginBottom: 3 }}>不做什么</div>
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {outScope.map((s, i) => (
                    <li key={i}>{s}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </GoalField>
      )}
      {(goal.success_criteria?.length ?? 0) > 0 && (
        <GoalField label="成功标准">
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {goal.success_criteria!.map((c, i) => (
              <li key={i} style={{ marginBottom: 3 }}>{c}</li>
            ))}
          </ul>
        </GoalField>
      )}
      {(goal.key_concepts?.length ?? 0) > 0 && (
        <GoalField label="关键概念">
          <div className="row gap6 wrap">
            {goal.key_concepts!.map((c, i) => (
              <span key={i} className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-2)' }}>
                {c}
              </span>
            ))}
          </div>
        </GoalField>
      )}
      {res && (
        <GoalField label="资源需求">
          <div className="col gap4">
            {res.compute && (
              <div className="row gap6">
                <Icon name="cpu" size={13} style={{ color: 'var(--text-3)', flexShrink: 0 }} />
                <span>算力：{res.compute}</span>
              </div>
            )}
            {(res.data?.length ?? 0) > 0 && (
              <div className="row gap6" style={{ alignItems: 'flex-start' }}>
                <Icon name="grid" size={13} style={{ color: 'var(--text-3)', flexShrink: 0, marginTop: 3 }} />
                <span>数据：{res.data!.join('、')}</span>
              </div>
            )}
            {res.time_weeks != null && (
              <div className="row gap6">
                <Icon name="clock" size={13} style={{ color: 'var(--text-3)', flexShrink: 0 }} />
                <span>预计约 {res.time_weeks} 周</span>
              </div>
            )}
          </div>
        </GoalField>
      )}
      {goal.smoke_plan && Object.keys(goal.smoke_plan).length > 0 && (
        <>
          <button className="btn btn-soft sm" onClick={() => setShowSmoke((v) => !v)}>
            <Icon
              name="chevron"
              size={12}
              style={{ transform: showSmoke ? 'rotate(90deg)' : 'none', transition: 'transform .15s' }}
            />
            最小验证实验 <span style={{ color: 'var(--text-4)', fontSize: 11 }}>1-3 天可出信号</span>
          </button>
          {showSmoke && (
            <pre className="codeblock" style={{ fontSize: 11, marginTop: 10 }}>
              {JSON.stringify(goal.smoke_plan, null, 2)}
            </pre>
          )}
        </>
      )}
    </div>
  );
}

/* ---------------- 依据文献列表（Idea 2.0） ---------------- */

const EVIDENCE_SOURCE_META: Record<IdeaEvidenceSource, { zh: string; bg: string; tx: string }> = {
  library: { zh: '库内', bg: 'var(--accent-soft)', tx: 'var(--accent-text)' },
  external: { zh: '外部', bg: 'var(--violet-bg)', tx: 'var(--violet-tx)' },
  signal: { zh: '信号', bg: 'var(--surface-3)', tx: 'var(--text-2)' },
};

function EvidenceCard({ idea }: { idea: IdeaDetail }) {
  const navigate = useNavigate();
  const evidence = idea.evidence ?? [];
  if (evidence.length === 0) return null;
  return (
    <div className="card card-pad">
      <span className="section-h" style={{ marginBottom: 12 }}>
        <Icon name="book" size={14} style={{ color: 'var(--accent)' }} />
        依据文献 <span className="en-label" style={{ fontSize: 11 }}>{evidence.length} 条</span>
      </span>
      <div className="col gap6">
        {evidence.map((ev, i) => {
          const meta = EVIDENCE_SOURCE_META[ev.source] ?? EVIDENCE_SOURCE_META.signal;
          const clickable = ev.source === 'library' && !!ev.paper_id;
          return (
            <div
              key={i}
              className={`row gap8${clickable ? ' hoverable' : ''}`}
              onClick={clickable ? () => navigate(`/wiki?paper=${ev.paper_id}`) : undefined}
              style={{
                border: '0.5px solid var(--border)',
                borderRadius: 9,
                padding: '8px 11px',
                background: 'var(--surface-2)',
                alignItems: 'flex-start',
              }}
            >
              <span className="pill sm" style={{ background: meta.bg, color: meta.tx, flexShrink: 0 }}>
                {meta.zh}
              </span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 12, lineHeight: 1.4, fontWeight: 600 }}>
                  {ev.source === 'external' && ev.url ? (
                    <a href={ev.url} target="_blank" rel="noreferrer noopener" onClick={(e) => e.stopPropagation()}>
                      {ev.title}
                      <Icon name="link" size={11} style={{ marginLeft: 4, verticalAlign: '-1px' }} />
                    </a>
                  ) : (
                    ev.title
                  )}
                </div>
                {ev.why && (
                  <div style={{ fontSize: 11.5, color: 'var(--text-3)', lineHeight: 1.5, marginTop: 2 }}>{ev.why}</div>
                )}
              </div>
              {clickable && <Icon name="chevron" size={12} style={{ color: 'var(--text-4)', flexShrink: 0, marginTop: 3 }} />}
            </div>
          );
        })}
      </div>
    </div>
  );
}

/* ---------------- 评审修订记录（Idea 2.0，只读时间线） ---------------- */

/** 从 session.payload 里容错取某轮的分数 / 修订摘要（后端结构宽松处理）。 */
function roundInfoOf(payload: Record<string, unknown> | null, round: number): { scores: string | null; summary: string | null } {
  const empty = { scores: null, summary: null };
  const roundsRaw = payload?.rounds;
  if (!Array.isArray(roundsRaw)) return empty;
  const item = roundsRaw.find(
    (r) => r && typeof r === 'object' && Number((r as Record<string, unknown>).round) === round,
  ) as Record<string, unknown> | undefined;
  if (!item) return empty;
  const summaryRaw = item.revision_summary ?? item.summary;
  const summary = typeof summaryRaw === 'string' && summaryRaw.trim() !== '' ? summaryRaw : null;
  const scoresRec = item.scores && typeof item.scores === 'object' ? (item.scores as Record<string, unknown>) : null;
  const scores = scoresRec
    ? SCORE_DIMS.filter((d) => typeof scoresRec[d.key] === 'number')
        .map((d) => `${d.zh} ${(scoresRec[d.key] as number).toFixed(1)}`)
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

  if (messagesQuery.isLoading) {
    return <div className="empty" style={{ padding: 16 }}>加载评审记录…</div>;
  }
  if (rounds.length === 0) {
    return <div className="empty" style={{ padding: 16 }}>暂无评审意见记录</div>;
  }
  return (
    <Timeline>
      {rounds.map(([round, msgs], i) => {
        const info = roundInfoOf(session.payload, round);
        return (
          <TimelineItem
            key={round}
            marker={String(round > 0 ? round : i + 1)}
            markerBg="var(--accent-soft)"
            markerColor="var(--accent-text)"
            last={i === rounds.length - 1}
          >
            <div className="row gap8" style={{ marginBottom: 8, flexWrap: 'wrap' }}>
              <span style={{ fontSize: 12.5, fontWeight: 650 }}>第 {round > 0 ? round : i + 1} 轮评审</span>
              {info.scores && (
                <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>{info.scores}</span>
              )}
            </div>
            {msgs.map((m) => (
              <DiscussionBubble key={m.id} msg={m} />
            ))}
            {info.summary && (
              <div
                style={{
                  fontSize: 11.5,
                  color: 'var(--text-2)',
                  lineHeight: 1.55,
                  background: 'var(--surface-2)',
                  borderRadius: 8,
                  padding: '7px 10px',
                }}
              >
                <b style={{ color: 'var(--text)' }}>本轮修订：</b>
                {info.summary}
              </div>
            )}
          </TimelineItem>
        );
      })}
    </Timeline>
  );
}

function RevisionTimeline({ ideaId }: { ideaId: string }) {
  const sessionsQuery = useQuery({
    queryKey: ['idea-sessions', ideaId],
    queryFn: () => api.listIdeaSessions(ideaId),
    retry: false,
  });
  const sessions = (sessionsQuery.data ?? []).filter((s) => s.target_type === 'idea_revision');
  if (sessions.length === 0) return null;
  return (
    <div className="card card-pad">
      <span className="section-h" style={{ marginBottom: 6 }}>
        <Icon name="shield" size={14} style={{ color: 'var(--accent)' }} />
        评审修订记录 <span className="en-label" style={{ fontSize: 11 }}>review &amp; revise</span>
      </span>
      <div style={{ fontSize: 11.5, color: 'var(--text-3)', lineHeight: 1.5, marginBottom: 14 }}>
        深度生成时，多位 AI 评审员逐轮提出必须修改项，作者 AI 修订后重评（只读记录）。
      </div>
      <div className="scroll" style={{ maxHeight: 460, overflowY: 'auto' }}>
        {sessions.map((s) => (
          <RevisionSessionBlock key={s.id} session={s} />
        ))}
      </div>
    </div>
  );
}

function ScoresCard({ idea }: { idea: IdeaDetail }) {
  const [showRationale, setShowRationale] = useState(false);
  const composite = compositeOf(idea.scores);
  return (
    <div className="card card-pad">
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 14 }}>
        <span className="section-h">
          <Icon name="scale" size={14} style={{ color: 'var(--accent)' }} />
          四维评分
        </span>
        {composite !== null && (
          <span className="pill" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
            composite <span className="mono" style={{ fontWeight: 700 }}>{composite.toFixed(1)}</span>
          </span>
        )}
      </div>
      {idea.scores ? (
        <>
          {SCORE_DIMS.map((d) => (
            <RubricBar key={d.key} label={`${d.zh} ${d.en}`} value={idea.scores![d.key]} />
          ))}
          {idea.score_rationale && (
            <>
              <button
                className="btn btn-soft sm"
                onClick={() => setShowRationale((v) => !v)}
                style={{ marginTop: 6 }}
              >
                <Icon
                  name="chevron"
                  size={12}
                  style={{ transform: showRationale ? 'rotate(90deg)' : 'none', transition: 'transform .15s' }}
                />
                打分理由 rationale
              </button>
              {showRationale && (
                <div className="col gap8" style={{ marginTop: 10 }}>
                  {SCORE_DIMS.map((d) => {
                    const r = idea.score_rationale?.[d.key];
                    if (!r) return null;
                    return (
                      <div
                        key={d.key}
                        style={{
                          fontSize: 12,
                          lineHeight: 1.55,
                          color: 'var(--text-2)',
                          background: 'var(--surface-2)',
                          borderRadius: 8,
                          padding: '8px 11px',
                        }}
                      >
                        <b style={{ color: 'var(--text)' }}>{d.zh} {d.en}</b> — {r}
                      </div>
                    );
                  })}
                </div>
              )}
            </>
          )}
        </>
      ) : (
        <span className="muted" style={{ fontSize: 12.5 }}>尚未打分 · not scored yet</span>
      )}
    </div>
  );
}

export function IdeaDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { openGates } = useShell();

  const ideaQuery = useQuery({
    queryKey: ['idea', id],
    queryFn: () => api.getIdea(id!),
    enabled: !!id,
    retry: false,
  });
  const idea = ideaQuery.data;

  function invalidateIdea() {
    void queryClient.invalidateQueries({ queryKey: ['idea', id] });
    void queryClient.invalidateQueries({ queryKey: ['ideas'] });
    void queryClient.invalidateQueries({ queryKey: ['leaderboard'] });
    void queryClient.invalidateQueries({ queryKey: ['forge-state'] });
  }

  const promoteMutation = useMutation({
    mutationFn: () => api.promoteIdea(id!),
    onSuccess: (gate) => {
      toast('已提交晋级审批，等待人工审批 · promotion approval created', 'ok');
      void queryClient.invalidateQueries({ queryKey: ['gates'] });
      invalidateIdea();
      openGates(gate.id);
    },
    onError: (e) => toast(`晋级失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const rejectMutation = useMutation({
    mutationFn: () => api.patchIdea(id!, { status: 'rejected' }),
    onSuccess: () => {
      toast('已淘汰该 idea · rejected', 'ok');
      invalidateIdea();
    },
    onError: (e) => toast(`淘汰失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  // —— [[paper:uuid]] → 库内论文链接（标题优先取依据文献 / 来源论文 / 目标 grounding） ——
  const paperTitles = useMemo(() => {
    const map = new Map<string, string>();
    for (const p of idea?.parent_papers ?? []) map.set(p.id, p.title);
    for (const ev of idea?.evidence ?? []) {
      if (ev.paper_id) map.set(ev.paper_id, ev.title);
    }
    return map;
  }, [idea]);
  const renderPaperRef = useCallback(
    (paperId: string) => (
      <span
        className="wikilink"
        role="link"
        tabIndex={0}
        title="打开库内论文"
        onClick={() => navigate(`/wiki?paper=${paperId}`)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') navigate(`/wiki?paper=${paperId}`);
        }}
      >
        {paperTitles.get(paperId) ?? `论文 ${paperId.slice(0, 8)}`}
      </span>
    ),
    [paperTitles, navigate],
  );

  if (ideaQuery.isLoading) {
    return (
      <div className="page fadeup">
        <div className="empty" style={{ padding: 80 }}>加载 idea 详情…</div>
      </div>
    );
  }
  if (ideaQuery.isError || !idea) {
    return (
      <div className="page fadeup">
        <div className="card">
          <EmptyState
            icon="x"
            title="无法加载 idea"
            desc="后端不可用、接口未就绪，或该 idea 不存在。"
            action={
              <button className="btn btn-ghost" onClick={() => navigate('/forge')}>
                <Icon name="arrow" size={14} style={{ transform: 'rotate(180deg)' }} />
                返回候选池
              </button>
            }
          />
        </div>
      </div>
    );
  }

  const actionable = idea.status === 'candidate' || idea.status === 'under_review';

  return (
    <div className="page fadeup">
      {/* 头部 */}
      <button className="btn btn-soft sm" onClick={() => navigate('/forge')} style={{ marginBottom: 16 }}>
        <Icon name="arrow" size={13} style={{ transform: 'rotate(180deg)' }} />
        返回候选池
      </button>
      <div className="row gap8" style={{ marginBottom: 8 }}>
        <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>{idea.id.slice(0, 8)}</span>
        <StatusPill status={idea.status} sm />
        <DepthBadge depth={idea.depth} />
        <ResearchTypeBadge type={idea.research_type} />
        <span className="pill sm" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
          Elo <span className="mono" style={{ fontWeight: 700 }}>{Math.round(idea.elo_rating)}</span>
        </span>
        <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', marginLeft: 'auto' }}>
          {fmtTime(idea.created_at)}
        </span>
      </div>
      <h1 style={{ fontSize: 22, fontWeight: 680, letterSpacing: '-0.015em', lineHeight: 1.3, margin: '0 0 6px' }}>
        {idea.title}
      </h1>
      <p style={{ fontSize: 13.5, color: 'var(--text-2)', lineHeight: 1.6, margin: '0 0 8px', maxWidth: 780 }}>
        {idea.summary}
      </p>
      {idea.seed_idea ? (
        <div className="row gap6" style={{ fontSize: 12, color: 'var(--text-3)', margin: '0 0 22px' }}>
          <Icon name="git" size={13} />
          深化自：
          <span
            className="hoverable"
            style={{ color: 'var(--accent-text)', fontWeight: 600 }}
            onClick={() => navigate(`/ideas/${idea.seed_idea!.id}`)}
          >
            {idea.seed_idea.title}
          </span>
          <Icon name="chevron" size={12} style={{ color: 'var(--text-4)' }} />
        </div>
      ) : (
        <div style={{ height: 14 }} />
      )}

      <div className="row gap20" style={{ alignItems: 'flex-start' }}>
        {/* —— 左：研究目标 + 正文 + 评审修订记录 + 讨论 —— */}
        <div className="col gap20" style={{ flex: 1.6, minWidth: 0 }}>
          {idea.goal && <GoalCard goal={idea.goal} />}
          <div className="card card-pad">
            <span className="section-h" style={{ marginBottom: 14 }}>
              <Icon name="file" size={14} style={{ color: 'var(--accent)' }} />
              {idea.depth === 'proposal' ? (
                <>
                  研究方案 <span className="en-label" style={{ fontSize: 11 }}>Research Proposal</span>
                </>
              ) : (
                <>
                  提案正文 <span className="en-label" style={{ fontSize: 11 }}>动机 · 方法 · 预期实验 · 风险</span>
                </>
              )}
            </span>
            {idea.content ? (
              <Markdown source={idea.content} renderPaperRef={renderPaperRef} />
            ) : (
              <span className="muted" style={{ fontSize: 12.5 }}>暂无正文</span>
            )}
          </div>
          <RevisionTimeline ideaId={idea.id} />
          <DiscussionPanel ideaId={idea.id} />
        </div>

        {/* —— 右：评分 / 依据文献 / parent papers / 操作 —— */}
        <div className="col gap16" style={{ flex: 1, minWidth: 0, maxWidth: 400 }}>
          <ScoresCard idea={idea} />
          <EvidenceCard idea={idea} />

          {/* parent papers */}
          <div className="card card-pad">
            <span className="section-h" style={{ marginBottom: 12 }}>
              <Icon name="book" size={14} style={{ color: 'var(--accent)' }} />
              来源论文 <span className="en-label" style={{ fontSize: 11 }}>parent papers</span>
            </span>
            {idea.parent_papers.length > 0 ? (
              <div className="col gap6">
                {idea.parent_papers.map((p) => (
                  <div
                    key={p.id}
                    className="row gap8 hoverable"
                    onClick={() => navigate(`/wiki?paper=${p.id}`)}
                    style={{
                      border: '0.5px solid var(--border)',
                      borderRadius: 9,
                      padding: '8px 11px',
                      background: 'var(--surface-2)',
                    }}
                  >
                    <Icon name="file" size={13} style={{ color: 'var(--text-3)', flexShrink: 0 }} />
                    <span style={{ fontSize: 12, lineHeight: 1.4, flex: 1 }}>{p.title}</span>
                    <Icon name="chevron" size={12} style={{ color: 'var(--text-4)', flexShrink: 0 }} />
                  </div>
                ))}
              </div>
            ) : (
              <span className="muted" style={{ fontSize: 12.5 }}>无来源论文记录</span>
            )}
          </div>

          {/* 操作 */}
          <div className="card card-pad" style={{ background: actionable ? 'var(--accent-soft)' : 'var(--surface-2)' }}>
            <div style={{ fontSize: 13.5, fontWeight: 650, marginBottom: 4 }}>晋级 / 淘汰</div>
            <div style={{ fontSize: 11.5, color: 'var(--text-2)', lineHeight: 1.55, marginBottom: 14 }}>
              {actionable
                ? '晋级会提交 idea_promotion 人工审批，审批通过后进入实验阶段；淘汰将其移出候选池。'
                : idea.status === 'promoted'
                  ? '该 idea 已晋级，无需再操作。'
                  : '该 idea 已淘汰。'}
            </div>
            <div className="row gap8">
              <button
                className="btn btn-primary"
                style={{ flex: 1, justifyContent: 'center' }}
                disabled={!actionable || promoteMutation.isPending}
                onClick={() => promoteMutation.mutate()}
              >
                <Icon name="arrow" size={14} />
                发起晋级 promote
              </button>
              <button
                className="btn btn-ghost"
                style={{ flex: 1, justifyContent: 'center' }}
                disabled={!actionable || rejectMutation.isPending}
                onClick={() => rejectMutation.mutate()}
              >
                <Icon name="x" size={14} />
                人工淘汰 reject
              </button>
            </div>
          </div>

          {/* 辩论记录入口 */}
          <button
            className="btn btn-ghost"
            onClick={() => navigate(`/review?tab=matches&idea=${idea.id}`)}
            style={{ justifyContent: 'center' }}
          >
            <Icon name="scale" size={14} />
            查看该 idea 的辩论记录 →
          </button>
        </div>
      </div>
    </div>
  );
}
