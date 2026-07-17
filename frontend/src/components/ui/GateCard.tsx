import { useState } from 'react';
import { Icon } from './Icon';
import { fmtTime } from '../../lib/format';
import { tr } from '../../lib/i18n';
import type { GateDecision, GateRead } from '../../lib/api';

const GATE_KIND: Record<string, { zh: string; en: string }> = {
  idea_promotion: { zh: '想法晋级审批', en: 'Idea promotion approval' },
  idea_goal: { zh: '研究目标确认', en: 'Research goal confirmation' },
  idea_pivot: { zh: '方向调整确认', en: 'Direction change confirmation' },
  compute_budget: { zh: '算力预算审批', en: 'Compute budget approval' },
  remote_write: { zh: '远程操作审批', en: 'Remote operation approval' },
  pr_push: { zh: '推送 PR', en: 'Push PR' },
  paper_submission: { zh: '论文投稿', en: 'Paper submission' },
};

/** gate kind → 当前语言标签（未收录的原样展示）。 */
export function gateKindLabel(kind: string): string {
  const m = GATE_KIND[kind];
  return m ? tr(m.zh, m.en) : kind;
}

function pickString(payload: Record<string, unknown> | null, keys: string[]): string | null {
  if (!payload) return null;
  for (const k of keys) {
    const v = payload[k];
    if (typeof v === 'string' && v.trim() !== '') return v;
  }
  return null;
}

/** 闸门标题：payload.title / summary，否则按 kind 显示。 */
export function gateTitle(g: GateRead): string {
  return pickString(g.payload, ['title', 'summary']) ?? gateKindLabel(g.kind);
}

/** 闸门描述：payload.description / reason / message。 */
export function gateDesc(g: GateRead): string | null {
  return pickString(g.payload, ['description', 'reason', 'message', 'detail']);
}

function payloadLines(payload: Record<string, unknown> | null): string[] {
  if (!payload) return [];
  return Object.entries(payload).map(
    ([k, v]) => `${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}`,
  );
}

/* ---------------- Idea 2.0 · 研究目标确认 / 方向调整确认 ---------------- */

const GOAL_TYPE: Record<string, { zh: string; en: string }> = {
  method: { zh: '方法', en: 'Method' },
  benchmark: { zh: '评测基准', en: 'Benchmark' },
  analysis: { zh: '分析', en: 'Analysis' },
  survey: { zh: '综述', en: 'Survey' },
  application: { zh: '应用', en: 'Application' },
  theory: { zh: '理论', en: 'Theory' },
};

function asRecord(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
}

function asStringArray(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string' && x.trim() !== '') : [];
}

function GoalRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ fontSize: 10.5, fontWeight: 700, color: 'var(--text-3)', marginBottom: 3 }}>{label}</div>
      <div style={{ fontSize: 12.5, lineHeight: 1.55 }}>{children}</div>
    </div>
  );
}

/** kind=idea_goal：payload.goal 结构化展示 + trace_summary 一行灰字。 */
function IdeaGoalView({ payload }: { payload: Record<string, unknown> | null }) {
  const goal = asRecord(payload?.goal);
  const trace = typeof payload?.trace_summary === 'string' && payload.trace_summary.trim() !== ''
    ? payload.trace_summary
    : null;
  const researchType = typeof goal?.research_type === 'string' ? goal.research_type : null;
  const task = typeof goal?.task === 'string' ? goal.task : null;
  const question = typeof goal?.question === 'string' ? goal.question : null;
  const objectives = asStringArray(goal?.objectives);
  const criteria = asStringArray(goal?.success_criteria);
  const groundingCount = Array.isArray(goal?.grounding) ? goal.grounding.length : 0;
  const typeMeta = researchType ? GOAL_TYPE[researchType] : undefined;
  const typeLabel = researchType ? (typeMeta ? tr(typeMeta.zh, typeMeta.en) : researchType) : null;
  return (
    <div style={{ marginTop: 12 }}>
      {typeLabel && (
        <div style={{ marginBottom: 10 }}>
          <span className="pill sm" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
            {tr(`研究类型：${typeLabel}`, `Research type: ${typeLabel}`)}
          </span>
        </div>
      )}
      {task && <GoalRow label={tr('研究任务', 'Research task')}>{task}</GoalRow>}
      {question && <GoalRow label={tr('核心问题', 'Core question')}>{question}</GoalRow>}
      {objectives.length > 0 && (
        <GoalRow label={tr('研究目标', 'Objectives')}>
          <ol style={{ margin: 0, paddingLeft: 18 }}>
            {objectives.map((o, i) => (
              <li key={i} style={{ marginBottom: 2 }}>{o}</li>
            ))}
          </ol>
        </GoalRow>
      )}
      {criteria.length > 0 && (
        <GoalRow label={tr('成功标准', 'Success criteria')}>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {criteria.map((c, i) => (
              <li key={i} style={{ marginBottom: 2 }}>{c}</li>
            ))}
          </ul>
        </GoalRow>
      )}
      {groundingCount > 0 && (
        <GoalRow label={tr('依据文献', 'Grounding papers')}>
          {tr(`共 ${groundingCount} 篇（详情见生成后的想法页）`, `${groundingCount} papers (details on the generated idea page)`)}
        </GoalRow>
      )}
      {trace && (
        <div style={{ fontSize: 11.5, color: 'var(--text-3)', lineHeight: 1.5, marginTop: 4 }}>
          {tr('探索过程：', 'Exploration trace: ')}{trace}
        </div>
      )}
    </div>
  );
}

const CMP_FIELD: Record<string, { zh: string; en: string }> = {
  similarity: { zh: '相似点', en: 'Similarity' },
  overlap: { zh: '重合点', en: 'Overlap' },
  difference: { zh: '差异', en: 'Difference' },
  why: { zh: '说明', en: 'Why' },
  reason: { zh: '原因', en: 'Reason' },
  verdict: { zh: '判定', en: 'Verdict' },
  note: { zh: '备注', en: 'Note' },
};

/** kind=idea_pivot：payload.reason + payload.comparisons（相似工作对比）。 */
function IdeaPivotView({ payload }: { payload: Record<string, unknown> | null }) {
  const reason = typeof payload?.reason === 'string' && payload.reason.trim() !== '' ? payload.reason : null;
  const comparisons = Array.isArray(payload?.comparisons) ? payload.comparisons : [];
  return (
    <div style={{ marginTop: 12 }}>
      {reason && (
        <div style={{ fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.55, marginBottom: 10 }}>{reason}</div>
      )}
      {comparisons.length > 0 && (
        <GoalRow label={tr(`高度重合的已有工作 · ${comparisons.length} 项`, `Highly overlapping prior work · ${comparisons.length}`)}>
          <div className="col gap6">
            {comparisons.map((c, i) => {
              const item = asRecord(c);
              if (!item) {
                return (
                  <div key={i} style={{ background: 'var(--surface-2)', borderRadius: 8, padding: '8px 11px' }}>
                    {typeof c === 'string' ? c : JSON.stringify(c)}
                  </div>
                );
              }
              const title = typeof item.title === 'string' ? item.title : null;
              const url = typeof item.url === 'string' ? item.url : null;
              const details = Object.entries(item).filter(
                ([k, v]) => !['title', 'url', 'paper_id', 'id'].includes(k) && typeof v === 'string' && v.trim() !== '',
              ) as [string, string][];
              return (
                <div key={i} style={{ background: 'var(--surface-2)', borderRadius: 8, padding: '8px 11px' }}>
                  {title && (
                    <div style={{ fontSize: 12, fontWeight: 650, marginBottom: details.length > 0 ? 4 : 0 }}>
                      {url ? (
                        <a href={url} target="_blank" rel="noreferrer noopener">{title}</a>
                      ) : (
                        title
                      )}
                    </div>
                  )}
                  {details.map(([k, v]) => {
                    const f = CMP_FIELD[k];
                    return (
                      <div key={k} style={{ fontSize: 11.5, color: 'var(--text-2)', lineHeight: 1.5 }}>
                        {f ? <b style={{ color: 'var(--text)' }}>{tr(`${f.zh}：`, `${f.en}: `)}</b> : null}
                        {v}
                      </div>
                    );
                  })}
                </div>
              );
            })}
          </div>
        </GoalRow>
      )}
      <div style={{ fontSize: 11.5, color: 'var(--text-3)', lineHeight: 1.5 }}>
        {tr(
          '批准 = AI 按你的意见调整研究方向后继续；拒绝 = 终止本次深度生成。',
          'Approve = AI adjusts the research direction per your comment and continues; reject = stop this deep generation.',
        )}
      </div>
    </div>
  );
}

export interface GateCardProps {
  gate: GateRead;
  expanded: boolean;
  onToggle: () => void;
  /** 审批动作（approve/reject + 可选 comment）。 */
  onDecide: (id: string, decision: GateDecision, comment?: string) => void;
  /** 正在提交审批（禁用按钮）。 */
  deciding?: boolean;
}

/** 审批闸门卡片：折叠头 + 展开后的 payload、意见输入与 approve/reject 操作。 */
export function GateCard({ gate: g, expanded, onToggle, onDecide, deciding }: GateCardProps) {
  const pending = g.status === 'pending';
  const [comment, setComment] = useState('');
  const isGoal = g.kind === 'idea_goal';
  const isPivot = g.kind === 'idea_pivot';
  const structured = isGoal || isPivot;
  const lines = structured ? [] : payloadLines(g.payload);
  const desc = structured ? null : gateDesc(g);
  return (
    <div className="card" style={{ overflow: 'hidden' }}>
      <div
        className="card-pad hoverable"
        onClick={onToggle}
        style={{ padding: '14px 16px', cursor: 'pointer', background: pending ? 'var(--accent-soft)' : 'var(--surface)' }}
      >
        <div className="row gap8" style={{ marginBottom: 6 }}>
          <span className="pill sm" style={{ background: 'var(--surface-3)' }}>
            {gateKindLabel(g.kind)}
          </span>
          {pending ? (
            <span className="pill sm" style={{ background: 'var(--warn-bg)', color: 'var(--warn-tx)' }}>
              <span className="dot" />
              {tr('待审批', 'Pending')}
            </span>
          ) : g.status === 'approved' ? (
            <span className="pill sm" style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>
              {tr('已批准', 'Approved')}
            </span>
          ) : (
            <span className="pill sm" style={{ background: 'var(--danger-bg)', color: 'var(--danger-tx)' }}>
              {tr('已拒绝', 'Rejected')}
            </span>
          )}
          <Icon
            name="chevDown"
            size={15}
            style={{ marginLeft: 'auto', color: 'var(--text-3)', transform: expanded ? 'rotate(180deg)' : 'none', transition: 'transform .2s' }}
          />
        </div>
        <div style={{ fontSize: 13.5, fontWeight: 650 }}>{gateTitle(g)}</div>
        <div className="row gap8" style={{ marginTop: 5 }}>
          <span className="mono muted" style={{ fontSize: 10.5 }}>{g.id.slice(0, 8)}</span>
          <span className="mono muted" style={{ fontSize: 10.5 }}>· {fmtTime(g.created_at)}</span>
        </div>
      </div>
      {expanded && (
        <div style={{ padding: '0 16px 16px', borderTop: '0.5px solid var(--border)' }}>
          {desc && <div style={{ fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.55, margin: '12px 0 0' }}>{desc}</div>}
          {isGoal && <IdeaGoalView payload={g.payload} />}
          {isPivot && <IdeaPivotView payload={g.payload} />}
          {lines.length > 0 && (
            <div className="codeblock" style={{ fontSize: 11, margin: '12px 0 0' }}>
              {lines.map((line, i) => (
                <div key={i}>{line}</div>
              ))}
            </div>
          )}
          {pending ? (
            <>
              <textarea
                className="textarea"
                placeholder={
                  isGoal
                    ? tr('可填写修改意见，AI 将按意见调整目标后继续', 'Optional comment — AI will adjust the goal accordingly and continue')
                    : isPivot
                      ? tr('可填写调整意见，AI 将按意见调整研究方向后继续', 'Optional comment — AI will adjust the direction accordingly and continue')
                      : tr('审批意见（可选）', 'Comment (optional)')
                }
                value={comment}
                onChange={(e) => setComment(e.target.value)}
                rows={2}
                style={{ width: '100%', marginTop: 12, minHeight: 52 }}
              />
              <div className="row gap8" style={{ marginTop: 10 }}>
                <button
                  className="btn btn-primary sm"
                  style={{ flex: 1, justifyContent: 'center' }}
                  disabled={deciding}
                  onClick={() => onDecide(g.id, 'approve', comment.trim() || undefined)}
                >
                  <Icon name="check" size={13} />
                  {isPivot ? tr('调整方向继续', 'Adjust and continue') : isGoal ? tr('确认目标', 'Confirm goal') : tr('批准', 'Approve')}
                </button>
                <button
                  className="btn btn-ghost sm"
                  style={{ flex: 1, justifyContent: 'center' }}
                  disabled={deciding}
                  onClick={() => onDecide(g.id, 'reject', comment.trim() || undefined)}
                >
                  <Icon name="x" size={13} />
                  {isPivot ? tr('终止', 'Stop') : isGoal ? tr('驳回', 'Reject') : tr('拒绝', 'Reject')}
                </button>
              </div>
            </>
          ) : (
            <div className="col gap6" style={{ marginTop: 12, fontSize: 11.5, color: 'var(--text-3)' }}>
              <div className="row gap8">
                <Icon name="check" size={13} style={{ color: g.status === 'approved' ? 'var(--ok-tx)' : 'var(--danger-tx)' }} />
                {g.status === 'approved'
                  ? tr(`于 ${fmtTime(g.decided_at)} 批准`, `Approved at ${fmtTime(g.decided_at)}`)
                  : tr(`于 ${fmtTime(g.decided_at)} 拒绝`, `Rejected at ${fmtTime(g.decided_at)}`)}
              </div>
              {g.comment && <div style={{ paddingLeft: 21 }}>{tr('意见：', 'Comment: ')}{g.comment}</div>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
