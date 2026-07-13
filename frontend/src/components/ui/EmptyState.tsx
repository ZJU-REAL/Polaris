import type { ReactNode } from 'react';
import { Icon, type IconName } from './Icon';

export interface EmptyStateProps {
  icon?: IconName;
  title: string;
  desc?: string;
  action?: ReactNode;
  /** 紧凑模式（列表内嵌）。 */
  compact?: boolean;
}

/** 空状态 / 降级提示卡片。 */
export function EmptyState({ icon = 'book', title, desc, action, compact }: EmptyStateProps) {
  return (
    <div style={{ textAlign: 'center', padding: compact ? '36px 20px' : '64px 32px' }}>
      <div
        style={{
          width: compact ? 38 : 48,
          height: compact ? 38 : 48,
          borderRadius: 12,
          margin: '0 auto 14px',
          background: 'var(--surface-3)',
          color: 'var(--text-3)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        <Icon name={icon} size={compact ? 18 : 22} />
      </div>
      <div style={{ fontSize: compact ? 13 : 15, fontWeight: 650, color: 'var(--text)' }}>{title}</div>
      {desc && (
        <div
          style={{
            fontSize: 12.5,
            color: 'var(--text-3)',
            marginTop: 6,
            lineHeight: 1.6,
            maxWidth: 420,
            marginLeft: 'auto',
            marginRight: 'auto',
          }}
        >
          {desc}
        </div>
      )}
      {action && <div style={{ marginTop: 18, display: 'flex', justifyContent: 'center' }}>{action}</div>}
    </div>
  );
}
