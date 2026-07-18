import type { ReactNode } from 'react';
import { Icon } from './Icon';
import { tr } from '../../lib/i18n';

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  sub?: ReactNode;
  children: ReactNode;
  /** 底部操作区（按钮行）。 */
  footer?: ReactNode;
  width?: number;
}

/** 居中对话框（scrim + panel）。 */
export function Modal({ open, onClose, title, sub, children, footer, width = 520 }: ModalProps) {
  if (!open) return null;
  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="modal" style={{ width: `min(${width}px, 92vw)` }} onClick={(e) => e.stopPropagation()}>
        <div
          className="row"
          style={{ padding: '16px 20px', borderBottom: '0.5px solid var(--border)', justifyContent: 'space-between' }}
        >
          <div>
            <div className="row gap8" style={{ fontSize: 14.5, fontWeight: 660 }}>
              {title}
            </div>
            {sub && <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 3 }}>{sub}</div>}
          </div>
          <button className="icon-btn" onClick={onClose} aria-label={tr('关闭', 'Close')}>
            <Icon name="x" size={15} />
          </button>
        </div>
        <div className="scroll" style={{ padding: '18px 20px', overflowY: 'auto', maxHeight: '64vh' }}>
          {children}
        </div>
        {footer && (
          <div className="row gap8" style={{ padding: '14px 20px', borderTop: '0.5px solid var(--border)', justifyContent: 'flex-end' }}>
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}
