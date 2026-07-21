import type { ReactNode } from 'react';
import { Icon } from './Icon';
import { tr } from '../../lib/i18n';

export interface DrawerProps {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  sub?: ReactNode;
  children: ReactNode;
}

/** 右侧滑出抽屉（scrim + panel）。 */
export function Drawer({ open, onClose, title, sub, children }: DrawerProps) {
  if (!open) return null;
  return (
    <>
      <div className="drawer-scrim" onClick={onClose} />
      <div className="drawer">
        <div className="row" style={{ padding: '18px 20px', borderBottom: '0.5px solid var(--border)', justifyContent: 'space-between' }}>
          <div>
            <div className="row gap8">{title}</div>
            {sub && <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 3 }}>{sub}</div>}
          </div>
          <button className="icon-btn" onClick={onClose} aria-label={tr('关闭', 'Close')}>
            <Icon name="x" size={16} />
          </button>
        </div>
        <div className="scroll" style={{ overflowY: 'auto', flex: 1, padding: '16px 18px' }}>
          {children}
        </div>
      </div>
    </>
  );
}
