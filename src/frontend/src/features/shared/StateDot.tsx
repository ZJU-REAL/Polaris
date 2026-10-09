import type { ReactNode } from 'react';

export type StateTone = 'neutral' | 'accent' | 'warning' | 'success' | 'danger';

const DOT_COLOR: Record<StateTone, string> = {
  neutral: 'var(--text-4)',
  accent: 'var(--accent)',
  warning: 'var(--warn)',
  success: 'var(--ok)',
  danger: 'var(--danger)',
};

/** 状态用「小圆点 + 一个词」表达，不用彩色胶囊（列表行最多两个徽标）。 */
export function StateDot({
  tone,
  children,
  title,
  onClick,
}: {
  tone: StateTone;
  children: ReactNode;
  title?: string;
  onClick?: () => void;
}) {
  return (
    <span
      className="row"
      title={title}
      onClick={onClick}
      style={{
        gap: 6,
        fontSize: 12,
        color: tone === 'danger' ? 'var(--danger-tx)' : tone === 'warning' ? 'var(--warn-tx)' : 'var(--text-2)',
        cursor: onClick ? 'pointer' : undefined,
        whiteSpace: 'nowrap',
      }}
    >
      <span style={{ width: 8, height: 8, borderRadius: '50%', background: DOT_COLOR[tone], flexShrink: 0 }} />
      {children}
    </span>
  );
}
