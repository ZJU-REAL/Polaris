import { useState } from 'react';
import { Icon } from './Icon';
import { fmtTime } from '../../lib/format';
import type { GateDecision, GateRead } from '../../lib/api';

export const GATE_KIND_ZH: Record<string, string> = {
  idea_promotion: 'Idea 晋级审批',
  compute_budget: '算力预算审批',
  remote_write: '远程操作审批',
  pr_push: '推送 PR',
  paper_submission: '论文投稿',
};

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
  return pickString(g.payload, ['title', 'summary']) ?? GATE_KIND_ZH[g.kind] ?? g.kind;
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
  const lines = payloadLines(g.payload);
  const desc = gateDesc(g);
  return (
    <div className="card" style={{ overflow: 'hidden' }}>
      <div
        className="card-pad hoverable"
        onClick={onToggle}
        style={{ padding: '14px 16px', cursor: 'pointer', background: pending ? 'var(--accent-soft)' : 'var(--surface)' }}
      >
        <div className="row gap8" style={{ marginBottom: 6 }}>
          <span className="pill sm" style={{ background: 'var(--surface-3)' }}>
            {GATE_KIND_ZH[g.kind] ?? g.kind}
          </span>
          {pending ? (
            <span className="pill sm" style={{ background: 'var(--warn-bg)', color: 'var(--warn-tx)' }}>
              <span className="dot" />
              待审批
            </span>
          ) : g.status === 'approved' ? (
            <span className="pill sm" style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>
              已批准
            </span>
          ) : (
            <span className="pill sm" style={{ background: 'var(--danger-bg)', color: 'var(--danger-tx)' }}>
              已拒绝
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
                placeholder="审批意见（可选） · comment"
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
                  批准 approve
                </button>
                <button
                  className="btn btn-ghost sm"
                  style={{ flex: 1, justifyContent: 'center' }}
                  disabled={deciding}
                  onClick={() => onDecide(g.id, 'reject', comment.trim() || undefined)}
                >
                  <Icon name="x" size={13} />
                  拒绝 reject
                </button>
              </div>
            </>
          ) : (
            <div className="col gap6" style={{ marginTop: 12, fontSize: 11.5, color: 'var(--text-3)' }}>
              <div className="row gap8">
                <Icon name="check" size={13} style={{ color: g.status === 'approved' ? 'var(--ok-tx)' : 'var(--danger-tx)' }} />
                于 {fmtTime(g.decided_at)} {g.status === 'approved' ? '批准' : '拒绝'}
              </div>
              {g.comment && <div style={{ paddingLeft: 21 }}>意见：{g.comment}</div>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
