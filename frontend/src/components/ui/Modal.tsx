import { useEffect, useRef, type ReactNode } from 'react';
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

// 打开中的弹窗栈：Esc 只关最上面（最后打开）的那个，避免嵌套弹窗被一起关掉
const openModals: object[] = [];

/** 居中对话框（scrim + panel）。 */
export function Modal({ open, onClose, title, sub, children, footer, width = 520 }: ModalProps) {
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    const token = {};
    openModals.push(token);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && openModals[openModals.length - 1] === token) {
        e.stopPropagation();
        onCloseRef.current();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
      const i = openModals.indexOf(token);
      if (i >= 0) openModals.splice(i, 1);
    };
  }, [open]);

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
