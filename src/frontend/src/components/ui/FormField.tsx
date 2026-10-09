import type { ReactNode } from 'react';
import { tr } from '../../lib/i18n';
import { HelpTip } from './HelpTip';

export interface FormFieldProps {
  label: string;
  /** 英文副标签 */
  en?: string;
  /** 字段下方一行提示（次要色）。超过一行的解释放进 help。 */
  hint?: string;
  /** 较长的解释：标签旁显示「?」，悬停查看。 */
  help?: string;
  error?: string | null;
  children: ReactNode;
  style?: React.CSSProperties;
}

/** 表单字段：标签 + 控件 + 提示/错误。控件请用 className="input"/"textarea"。 */
export function FormField({ label, en, hint, help, error, children, style }: FormFieldProps) {
  return (
    <div className="field" style={style}>
      <label className="field-label">
        {tr(label, en)}
        {help && <HelpTip text={help} />}
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
