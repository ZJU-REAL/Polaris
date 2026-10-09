import type { ReactNode } from 'react';
import { tr } from '../../lib/i18n';
import { HelpTip } from './HelpTip';

export interface FormFieldProps {
  /** 标签文字。推荐直接传 tr(zh, en) 翻译好的结果，不再传 en。 */
  label: string;
  /**
   * 旧写法：label 传中文原文、en 传英文版本。只有 label 含中文时才会用它——
   * label 已经翻译好时（英文界面下是英文），en 常常是字段名（如 "max_runs"），
   * 不能拿来顶替标签。
   */
  en?: string;
  /** 字段下方一行提示（次要色）。超过一行的解释放进 help。 */
  hint?: string;
  /** 较长的解释：标签旁显示「?」，悬停查看。 */
  help?: string;
  error?: string | null;
  children: ReactNode;
  style?: React.CSSProperties;
}

const CJK = /[\u3400-\u9fff]/;

/** label 未翻译（含中文）且给了 en 时才走 tr；否则原样显示 label。 */
export function fieldLabel(label: string, en?: string): string {
  return en && CJK.test(label) ? tr(label, en) : label;
}

/** 表单字段：标签 + 控件 + 提示/错误。控件请用 className="input"/"textarea"。 */
export function FormField({ label, en, hint, help, error, children, style }: FormFieldProps) {
  return (
    <div className="field" style={style}>
      <label className="field-label">
        {fieldLabel(label, en)}
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
