import { useId } from 'react';

export interface HelpTipProps {
  /** 解释文字：一两句，悬停或键盘聚焦时显示。 */
  text: string;
}

/**
 * 「?」帮助提示：放在标签旁边，把较长的解释收进悬浮提示，
 * 让表单本身只留一行提示（见 WRITING.md）。键盘可聚焦，读屏读出解释。
 * 弄错就会出问题的设置（API 地址、定时）仍应保留一行看得见的提示，不要只藏在这里。
 */
export function HelpTip({ text }: HelpTipProps) {
  const id = useId();
  return (
    <span className="help-tip" tabIndex={0} aria-describedby={id} role="img" aria-label="?">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
        strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <circle cx="12" cy="12" r="9.5" />
        <path d="M9.6 9.3a2.5 2.5 0 0 1 4.8.9c0 1.7-2.4 2.2-2.4 3.6" />
        <circle cx="12" cy="17" r="0.6" fill="currentColor" />
      </svg>
      <span className="help-tip-bubble" role="tooltip" id={id}>{text}</span>
    </span>
  );
}
