import type { ReactNode } from 'react';
import { Modal } from './Modal';
import { tr } from '../../lib/i18n';

export interface ConfirmModalProps {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  /** 说明文字（正文）。 */
  message: ReactNode;
  confirmText?: string;
  /** 危险操作（删除等）：确认按钮红色。 */
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
}

/** 体系内确认对话框（替代 window.confirm）。 */
export function ConfirmModal({
  open,
  onClose,
  title,
  message,
  confirmText = tr('确定', 'OK'),
  danger,
  busy,
  onConfirm,
}: ConfirmModalProps) {
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      width={400}
      footer={
        <>
          <button className="btn btn-ghost sm" onClick={onClose} disabled={busy}>
            {tr('取消', 'Cancel')}
          </button>
          <button
            className={`btn sm ${danger ? 'btn-danger' : 'btn-primary'}`}
            onClick={onConfirm}
            disabled={busy}
            autoFocus
          >
            {busy ? tr('处理中…', 'Working…') : confirmText}
          </button>
        </>
      }
    >
      <div style={{ fontSize: 13, color: 'var(--text-2)', lineHeight: 1.7 }}>{message}</div>
    </Modal>
  );
}
