import type { ReactNode } from 'react';
import { tr } from '../../lib/i18n';

export interface FormFieldProps {
  label: string;
  /** 英文副标签 */
  en?: string;
  hint?: string;
  error?: string | null;
  children: ReactNode;
  style?: React.CSSProperties;
}

/** 表单字段：中英标签 + 控件 + 提示/错误。控件请用 className="input"/"textarea"。 */
export function FormField({ label, en, hint, error, children, style }: FormFieldProps) {
  return (
    <div className="field" style={style}>
      <label className="field-label">
        {tr(label, en)}
      </label>
      {children}
      {error ? (
        <div className="field-error">{error}</div>
      ) : hint ? (
        <div className="field-hint">{hint}</div>
      ) : null}
    </div>
  );
}
