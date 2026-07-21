import { useEffect, useState, type ReactNode } from 'react';
import { Modal } from './Modal';

export interface PromptModalProps {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  /** 输入框上方说明（可选）。 */
  label?: ReactNode;
  placeholder?: string;
  /** 打开时的初始值。 */
  initial?: string;
  submitText?: string;
  /** 等宽字体（文件路径等）。 */
  mono?: boolean;
  busy?: boolean;
  /** 提交（trim 后非空才回调）。 */
  onSubmit: (value: string) => void;
}

/** 体系内单行输入对话框（替代 window.prompt）。 */
export function PromptModal({
  open,
  onClose,
  title,
  label,
  placeholder,
  initial = '',
  submitText = '确定',
  mono,
  busy,
  onSubmit,
}: PromptModalProps) {
  const [value, setValue] = useState(initial);
  useEffect(() => {
    if (open) setValue(initial);
  }, [open, initial]);

  function submit() {
    const v = value.trim();
    if (v) onSubmit(v);
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      width={460}
      footer={
        <>
          <button className="btn btn-ghost sm" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button
            className="btn btn-primary sm"
            onClick={submit}
            disabled={busy || !value.trim()}
          >
            {busy ? '处理中…' : submitText}
          </button>
        </>
      }
    >
      {label && <div style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 8 }}>{label}</div>}
      <input
        className={`input${mono ? ' mono' : ''}`}
        style={{ width: '100%', height: 32, fontSize: 12.5 }}
        autoFocus
        placeholder={placeholder}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') submit();
          if (e.key === 'Escape') onClose();
        }}
      />
    </Modal>
  );
}
