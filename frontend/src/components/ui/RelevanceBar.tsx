import { tr } from '../../lib/i18n';

/** 相关度条（0-1），按阈值显色：≥0.85 绿 / ≥0.7 蓝 / 其余 黄。 */
export interface RelevanceBarProps {
  /** 0-1；null/undefined 显示未打分占位 */
  value: number | null | undefined;
  width?: number;
}

export function RelevanceBar({ value, width = 92 }: RelevanceBarProps) {
  if (value === null || value === undefined) {
    return (
      <div className="row gap8" style={{ width, flexShrink: 0 }}>
        <div className="bar" style={{ flex: 1, opacity: 0.5 }}>
          <i style={{ width: 0 }} />
        </div>
        <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>
          —
        </span>
      </div>
    );
  }
  const v = Math.max(0, Math.min(1, value));
  const color = v >= 0.85 ? 'var(--ok)' : v >= 0.7 ? 'var(--accent)' : 'var(--warn)';
  return (
    <div className="row gap8" style={{ width, flexShrink: 0 }} title={tr(`相关度 ${v.toFixed(2)}`, `Relevance ${v.toFixed(2)}`)}>
      <div className="bar" style={{ flex: 1 }}>
        <i style={{ width: `${v * 100}%`, background: color }} />
      </div>
      <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>
        {v.toFixed(2)}
      </span>
    </div>
  );
}
