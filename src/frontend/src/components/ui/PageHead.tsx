import type { ReactNode } from 'react';

export interface PageHeadProps {
  eyebrow: string;
  title: string;
  sub?: string;
  right?: ReactNode;
  /** 紧凑：无副标题时收紧与下方内容的间距 */
  dense?: boolean;
}

/** Eyebrow + 大标题 + 副标题的页头（调用方用 tr() 传入当前语言文案）。 */
export function PageHead({ eyebrow, title, sub, right, dense }: PageHeadProps) {
  return (
    // 窄屏下操作区改到标题下方（见 global.css）：右侧按钮不压缩，标题的
    // flex-basis 又是 0，同排会把标题挤成一栏窄条、逐字换行。
    // 间距写在 CSS 里而不是内联：内联优先级高于样式表，窄屏改不动。
    <div className="row page-head" style={{ alignItems: 'flex-start', marginBottom: dense ? 14 : 26 }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="h-eyebrow">{eyebrow}</div>
        <h1 className="h-title">{title}</h1>
        {sub && <p className="h-sub">{sub}</p>}
      </div>
      {right && <div className="row gap10 page-head-actions">{right}</div>}
    </div>
  );
}
