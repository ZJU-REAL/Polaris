import { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { StatusPill } from '../../components/ui/StatusPill';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { Markdown } from '../../lib/markdown';
import { fmtTime } from '../../lib/format';
import { api, type IdeaDetail } from '../../lib/api';
import { useShell } from '../../app/AppShell';
import { DiscussionPanel } from '../review/DiscussionPanel';
import { compositeOf, RubricBar, SCORE_DIMS } from './ideaShared';

/* ============================================================
   /ideas/:id — idea 详情（M3）
   content markdown、四维分数 + rationale 折叠、parent papers
   链接（跳 wiki）、晋级 / 人工淘汰、底部人机讨论区。
   ============================================================ */

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
      <p style={{ fontSize: 13.5, color: 'var(--text-2)', lineHeight: 1.6, margin: '0 0 22px', maxWidth: 780 }}>
        {idea.summary}
      </p>

      <div className="row gap20" style={{ alignItems: 'flex-start' }}>
        {/* —— 左：正文 + 讨论 —— */}
        <div className="col gap20" style={{ flex: 1.6, minWidth: 0 }}>
          <div className="card card-pad">
            <span className="section-h" style={{ marginBottom: 14 }}>
              <Icon name="file" size={14} style={{ color: 'var(--accent)' }} />
              提案正文 <span className="en-label" style={{ fontSize: 11 }}>动机 · 方法 · 预期实验 · 风险</span>
            </span>
            {idea.content ? (
              <Markdown source={idea.content} />
            ) : (
              <span className="muted" style={{ fontSize: 12.5 }}>暂无正文</span>
            )}
          </div>
          <DiscussionPanel ideaId={idea.id} />
        </div>

        {/* —— 右：评分 / parent papers / 操作 —— */}
        <div className="col gap16" style={{ flex: 1, minWidth: 0, maxWidth: 400 }}>
          <ScoresCard idea={idea} />

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
