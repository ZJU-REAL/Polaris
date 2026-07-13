import type { ReactNode } from 'react';

export interface TimelineItemProps {
  /** 左侧圆点内容（序号/图标）。 */
  marker: ReactNode;
  /** 圆点配色（背景/前景），默认中性。 */
  markerBg?: string;
  markerColor?: string;
  /** 是否最后一项（不画竖线）。 */
  last?: boolean;
  children: ReactNode;
}

/** 垂直时间线的一项：左侧 rail（圆点 + 竖线）+ 右侧内容。 */
export function TimelineItem({ marker, markerBg, markerColor, last, children }: TimelineItemProps) {
  return (
    <div className="tl-item">
      <div className="tl-rail">
        <div
          className="tl-dot mono"
          style={{
            background: markerBg ?? 'var(--surface-2)',
            color: markerColor ?? 'var(--text-3)',
          }}
        >
          {marker}
        </div>
        {!last && <div className="tl-line" />}
      </div>
      <div style={{ flex: 1, minWidth: 0, paddingBottom: last ? 0 : 14 }}>{children}</div>
    </div>
  );
}

export function Timeline({ children }: { children: ReactNode }) {
  return <div className="tl">{children}</div>;
}
