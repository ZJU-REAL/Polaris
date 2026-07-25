import type { ReactNode } from 'react';

export interface PageHeadProps {
  /** 面包屑。**不再渲染** —— 侧栏和顶栏已经指明当前位置。prop 保留，免改调用点。 */
  eyebrow?: string;
  /** 大标题。**不再渲染** —— 同上，侧栏选中项就是标题。prop 保留，免改调用点。 */
  title?: string;
  /** 仍会渲染：这里只剩「没选课题」这类真提示，以及库详情页的方向描述。 */
  sub?: string;
  right?: ReactNode;
  /** 紧凑：收紧与下方内容的间距 */
  dense?: boolean;
}

/**
 * 页头：标题与面包屑都交给导航去表达，这里只剩副标题与操作区。
 * 两者都没有时不渲染任何东西——留一个空 div 会在页面顶部留下一条孤儿空白。
 *
 * 有标签行（Segmented）的页面不该再用 `right`：操作应当并进标签行，
 * 免得顶部多出一整行只放按钮。
 */
export function PageHead({ sub, right, dense }: PageHeadProps) {
  if (!sub && !right) return null;
  return (
    // 窄屏下操作区改到副标题下方（见 global.css）：右侧按钮不压缩，左栏的
    // flex-basis 又是 0，同排会把文字挤成一栏窄条、逐字换行。
    // 间距写在 CSS 里而不是内联：内联优先级高于样式表，窄屏改不动。
    <div className="row page-head" style={{ alignItems: 'flex-start', marginBottom: dense ? 12 : 16 }}>
      <div style={{ flex: 1, minWidth: 0 }}>{sub && <p className="h-sub">{sub}</p>}</div>
      {right && <div className="row gap10 page-head-actions">{right}</div>}
    </div>
  );
}
