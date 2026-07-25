import type { ReactNode } from 'react';

export interface PageHeadProps {
  /**
   * 面包屑（如 "Polaris · My Library"）。**不再渲染** —— 侧栏和顶栏已经指明了
   * 当前位置，页头再写一遍只是把标题往下推。保留 prop 是为了不改 18 个调用点。
   */
  eyebrow?: string;
  title: string;
  sub?: string;
  right?: ReactNode;
  /** 紧凑：无副标题时收紧与下方内容的间距 */
  dense?: boolean;
}

/** 大标题 + 可选副标题的页头（调用方用 tr() 传入当前语言文案）。 */
export function PageHead({ title, sub, right, dense }: PageHeadProps) {
  return (
    // 窄屏下操作区改到标题下方（见 global.css）：右侧按钮不压缩，标题的
    // flex-basis 又是 0，同排会把标题挤成一栏窄条、逐字换行。
    // 间距写在 CSS 里而不是内联：内联优先级高于样式表，窄屏改不动。
    <div className="row page-head" style={{ alignItems: 'flex-start', marginBottom: dense ? 12 : 18 }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <h1 className="h-title">{title}</h1>
        {sub && <p className="h-sub">{sub}</p>}
      </div>
      {right && <div className="row gap10 page-head-actions">{right}</div>}
    </div>
  );
}
