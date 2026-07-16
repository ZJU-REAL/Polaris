import { Fragment } from 'react';
import { Icon, type IconName } from './Icon';

export interface PipelineStage {
  key: string;
  path: string;
  no: string;
  icon: IconName;
  zh: string;
  en: string;
  /** 阶段计数（null = 统计不可用，显示 —） */
  count: number | null;
  /** 当前有任务运行中的阶段 */
  running?: boolean;
}

export interface PipelineFlowProps {
  stages: PipelineStage[];
  /** 当前自动运行方向的展示名 */
  directionLabel: string;
  onNavigate: (path: string) => void;
}

/** 端到端研究流水线（00-05 阶段卡 + 箭头连接）。 */
export function PipelineFlow({ stages, directionLabel, onNavigate }: PipelineFlowProps) {
  return (
    <div className="card card-pad" style={{ marginBottom: 24 }}>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 18 }}>
        <div className="row gap10">
          <span className="section-h">
            <Icon name="layers" size={16} style={{ color: 'var(--accent)' }} />
            端到端研究流水线
          </span>
          <span className="muted" style={{ fontSize: 12 }}>
            End-to-end pipeline
          </span>
        </div>
        <span className="pill">
          <span className="dot" style={{ background: 'var(--ok)' }} />
          当前方向 · {directionLabel}
        </span>
      </div>
      <div className="row" style={{ alignItems: 'stretch', gap: 0 }}>
        {stages.map((s, idx) => (
          <Fragment key={s.key}>
            <div
              className="hoverable"
              onClick={() => onNavigate(s.path)}
              style={{
                flex: 1,
                borderRadius: 12,
                padding: '16px 12px 14px',
                textAlign: 'center',
                border: '0.5px solid var(--border)',
                background: s.running ? 'var(--accent-soft)' : 'var(--surface-2)',
                position: 'relative',
                cursor: 'pointer',
              }}
            >
              <div style={{ position: 'absolute', top: 8, left: 10, fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-4)' }}>
                {s.no}
              </div>
              <div
                style={{
                  width: 38,
                  height: 38,
                  margin: '4px auto 10px',
                  borderRadius: 10,
                  background: s.running ? 'var(--accent)' : 'var(--surface)',
                  color: s.running ? '#fff' : 'var(--text-2)',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  boxShadow: 'var(--shadow-card)',
                }}
              >
                <Icon name={s.icon} size={19} />
              </div>
              <div style={{ fontSize: 12.5, fontWeight: 650 }}>{s.zh}</div>
              <div style={{ fontSize: 10.5, color: 'var(--text-3)', marginTop: 1 }}>{s.en}</div>
              <div
                style={{
                  marginTop: 9,
                  fontFamily: 'var(--mono)',
                  fontSize: 20,
                  fontWeight: 700,
                  color: s.running ? 'var(--accent-text)' : 'var(--text)',
                }}
              >
                {s.count ?? '—'}
              </div>
              {s.running && (
                <div
                  className="pulse"
                  style={{ position: 'absolute', bottom: 8, left: 0, right: 0, fontSize: 9.5, color: 'var(--accent-text)', fontWeight: 600 }}
                >
                  ● 运行中
                </div>
              )}
            </div>
            {idx < stages.length - 1 && (
              <div className="row" style={{ alignItems: 'center', width: 26, justifyContent: 'center', flexShrink: 0 }}>
                <svg width="22" height="14" viewBox="0 0 22 14">
                  <path
                    d="M1 7h17M14 2l5 5-5 5"
                    fill="none"
                    stroke="var(--border-strong)"
                    strokeWidth="1.6"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </div>
            )}
          </Fragment>
        ))}
      </div>
    </div>
  );
}
