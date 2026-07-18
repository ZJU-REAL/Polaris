import type { ReactNode } from 'react';
import { Icon } from './Icon';
import { tr } from '../../lib/i18n';

export interface AccordionSectionProps {
  /** 中文标题 */
  title: string;
  /** 英文副标题 */
  en?: string;
  open: boolean;
  onToggle: () => void;
  /** 右侧小标记，如「AI 已填」「3 条」 */
  badge?: string;
  children: ReactNode;
}

/** 可折叠分区（受控）。标题遵循 BiTitle 中英双语风格。 */
export function AccordionSection({ title, en, open, onToggle, badge, children }: AccordionSectionProps) {
  return (
    <div className="card" style={{ overflow: 'hidden' }}>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        style={{
          width: '100%',
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          padding: '13px 16px',
          background: 'transparent',
          border: 'none',
          cursor: 'pointer',
          textAlign: 'left',
          fontFamily: 'var(--sans)',
        }}
      >
        <Icon
          name="chevron"
          size={14}
          style={{
            color: 'var(--text-3)',
            transform: open ? 'rotate(90deg)' : 'none',
            transition: 'transform .15s',
            flexShrink: 0,
          }}
        />
        <span style={{ flex: 1, minWidth: 0 }}>
          <span style={{ fontSize: 13.5, fontWeight: 650, color: 'var(--text)', letterSpacing: '-0.01em' }}>
            {tr(title, en)}
          </span>
        </span>
        {badge && (
          <span
            className="mono"
            style={{
              fontSize: 10.5,
              padding: '2px 8px',
              borderRadius: 999,
              background: 'var(--accent-soft)',
              color: 'var(--accent-text)',
              flexShrink: 0,
            }}
          >
            {badge}
          </span>
        )}
      </button>
      {open && <div style={{ padding: '2px 16px 16px 40px' }}>{children}</div>}
    </div>
  );
}
