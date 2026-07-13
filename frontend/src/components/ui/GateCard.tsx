import { Icon } from './Icon';
import { GATE_TYPE_ZH, type Gate, type GateStatus } from '../../lib/mock';

export interface GateCardProps {
  gate: Gate;
  expanded: boolean;
  onToggle: () => void;
  onDecide: (id: string, status: GateStatus) => void;
}

/** 审批闸门卡片：折叠头 + 展开的 payload 与 approve/reject 操作。 */
export function GateCard({ gate: g, expanded, onToggle, onDecide }: GateCardProps) {
  const pending = g.status === 'pending';
  return (
    <div
      className="card"
      style={{ overflow: 'hidden', borderColor: g.urgent && pending ? 'var(--accent-soft-2)' : 'var(--border)' }}
    >
      <div
        className="card-pad hoverable"
        onClick={onToggle}
        style={{ padding: '14px 16px', cursor: 'pointer', background: g.urgent && pending ? 'var(--accent-soft)' : 'var(--surface)' }}
      >
        <div className="row gap8" style={{ marginBottom: 6 }}>
          <span className="pill sm" style={{ background: 'var(--surface-3)' }}>
            {GATE_TYPE_ZH[g.type]}
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
        <div style={{ fontSize: 13.5, fontWeight: 650 }}>{g.title}</div>
        <div className="row gap8" style={{ marginTop: 5 }}>
          <span className="mono muted" style={{ fontSize: 10.5 }}>{g.id}</span>
          <span className="mono muted" style={{ fontSize: 10.5 }}>· {g.idea}</span>
        </div>
      </div>
      {expanded && (
        <div style={{ padding: '0 16px 16px', borderTop: '0.5px solid var(--border)' }}>
          <div style={{ fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.55, margin: '12px 0' }}>{g.desc}</div>
          <div className="codeblock" style={{ fontSize: 11, marginBottom: 14 }}>
            {g.payload.map((line, i) => (
              <div key={i}>{line}</div>
            ))}
          </div>
          {pending ? (
            <div className="row gap8">
              <button
                className="btn btn-primary sm"
                style={{ flex: 1, justifyContent: 'center' }}
                onClick={() => onDecide(g.id, 'approved')}
              >
                <Icon name="check" size={13} />
                批准 approve
              </button>
              <button
                className="btn btn-ghost sm"
                style={{ flex: 1, justifyContent: 'center' }}
                onClick={() => onDecide(g.id, 'rejected')}
              >
                <Icon name="x" size={13} />
                拒绝 reject
              </button>
            </div>
          ) : (
            <div className="row gap8" style={{ fontSize: 11.5, color: 'var(--text-3)' }}>
              <Icon name="check" size={13} style={{ color: 'var(--ok-tx)' }} />
              由 {g.decidedBy ?? 'you'} 于 {g.decidedAt ?? '刚刚'} {g.status === 'approved' ? '批准' : '拒绝'} · 流水线已继续
            </div>
          )}
        </div>
      )}
    </div>
  );
}
