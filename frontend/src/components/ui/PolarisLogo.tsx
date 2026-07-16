import { useId } from 'react';

/* ============================================================
   Polaris 品牌标识（据 logo.png 逐像素测量复刻的 SVG）：
   - PolarisMark：北极星（四角星）+ 三条从彩色圆点汇聚向星的
     月牙形轨迹（两端收细）；底部轨迹为紫→蓝→青渐变。
   - PolarisWordmark：几何方圆风格的 Polaris 字标（l/r 竖干
     顶部斜切、P/a 碗内青色短横、i 的青色圆点）。
   品牌色（取自原图）：藏青 #002161 / 青 #00D8FE / 紫 #7438F0。
   ============================================================ */

export const POLARIS_NAVY = '#002161';
export const POLARIS_CYAN = '#00D8FE';
export const POLARIS_PURPLE = '#7438F0';

/** 图形标（正方形）；size 为渲染像素。 */
export function PolarisMark({ size = 26, title = 'Polaris' }: { size?: number; title?: string }) {
  const gid = useId(); // 渐变 id 按实例唯一，避免多处渲染时互相引用
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="none"
      role="img"
      aria-label={title}
      style={{ display: 'block', flexShrink: 0 }}
    >
      <defs>
        <linearGradient id={gid} x1="26" y1="58" x2="54" y2="25" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor={POLARIS_PURPLE} />
          <stop offset="0.2" stopColor="#2E7CF6" />
          <stop offset="0.65" stopColor="#00B9FE" />
          <stop offset="1" stopColor={POLARIS_CYAN} />
        </linearGradient>
      </defs>
      {/* 三条楔形轨迹（按原图测量：起始厚 3.8-4.4、尖端收细贴星），下为渐变 */}
      <path d="M11 28.2 C24 27.4 36.5 23.1 44.7 16.8 L45.2 17.6 C37 26.6 24 31.2 11 32.6 A2.2 2.2 0 0 1 11 28.2 Z" fill={POLARIS_NAVY} />
      <path d="M14 42.9 C27.5 40.0 39.5 32.6 47.7 22.3 L48.5 23.3 C40.6 36.6 28 44.5 14 47.7 A2.4 2.4 0 0 1 14 42.9 Z" fill={POLARIS_NAVY} />
      <path d="M25.4 54.7 C34.5 51.7 46.3 42.5 52.9 23.8 L54.3 24.7 C47.8 45.6 36 56.4 25.4 59.7 A2.5 2.5 0 0 1 25.4 54.7 Z" fill={`url(#${gid})`} />
      {/* 北极星（四角星，凹边） */}
      <path
        d="M54 0 C55.5 6.5 57.8 8.8 64 10.3 C57.8 11.8 55.5 14.1 54 20.6 C52.5 14.1 50.2 11.8 44 10.3 C50.2 8.8 52.5 6.5 54 0 Z"
        fill={POLARIS_NAVY}
      />
      {/* 起点三色圆点 */}
      <circle cx="4.7" cy="28.8" r="4" fill={POLARIS_NAVY} />
      <circle cx="5.3" cy="45.5" r="4.5" fill={POLARIS_CYAN} />
      <circle cx="18.2" cy="59.1" r="4.2" fill={POLARIS_PURPLE} />
    </svg>
  );
}

const WORDMARK_RATIO = 132 / 34;

/** Polaris 字标；height 为渲染像素高，宽度按比例。 */
export function PolarisWordmark({ height = 16, title = 'Polaris' }: { height?: number; title?: string }) {
  return (
    <svg
      width={height * WORDMARK_RATIO}
      height={height}
      viewBox="0 0 132 34"
      fill="none"
      role="img"
      aria-label={title}
      style={{ display: 'block', flexShrink: 0 }}
    >
      <g stroke={POLARIS_NAVY} strokeWidth="4">
        {/* P：一笔连画 —— 竖干直上、左上角平滑大圆角转入碗顶 */}
        <path d="M4.95 31.7 V10.95 A7 7 0 0 1 11.95 3.95 H16.65 A7 7 0 0 1 23.65 10.95 V13.55 A7 7 0 0 1 16.65 20.55 H4.95" />
        {/* o */}
        <rect x="29.05" y="13.35" width="17.9" height="16.7" rx="4.8" />
        {/* a 下碗（底笔与右干基线齐平，右下角成方角） */}
        <path d="M73.4 21.8 H67.0 Q62.55 21.8 62.55 25.8 Q62.55 29.7 67.5 29.7 H78.6" />
        {/* r：一体 Γ 形 */}
        <path d="M87.45 31.7 V21.3 Q87.45 13.35 95.4 13.35 H99.3" />
        {/* i 竖干 */}
        <path d="M105.3 11.7 V31.7" />
        {/* s：上下平直横笔 + 斜向中段（端头斜切见下方三角补片） */}
        <path d="M126.0 15.35 H119.2 Q113.9 15.35 113.9 18.3 C113.9 20.6 115.9 21.7 119.2 22.25 C123.2 22.95 126.7 24.0 126.7 26.8 Q126.7 29.7 121.7 29.7 H114.4" />
      </g>
      {/* s 端头斜切补片：右上端与左下端 */}
      <path d="M126.0 13.35 L128.7 17.35 H126.0 Z" fill={POLARIS_NAVY} />
      <path d="M114.4 31.7 L111.9 27.7 H114.4 Z" fill={POLARIS_NAVY} />
      {/* l：顶部斜切竖干 */}
      <path d="M52.9 5.6 L56.9 3.0 V31.7 H52.9 Z" fill={POLARIS_NAVY} />
      {/* a 上横臂 + 右干（填充轮廓：左端斜切、右下端斜切） */}
      <path d="M63.0 11.7 H74.9 C79.2 11.7 80.6 13.9 80.6 19.2 V31.7 H76.6 V18.8 C76.6 16.4 75.4 15.7 73.4 15.7 H65.4 Z" fill={POLARIS_NAVY} />
      {/* 青色细节：P/a 碗内短横 + i 点 */}
      <rect x="10.6" y="12.4" width="6.8" height="1.9" rx="0.95" fill={POLARIS_CYAN} />
      <rect x="68.1" y="25.3" width="5.1" height="1.5" rx="0.75" fill={POLARIS_CYAN} />
      <circle cx="105.4" cy="5.2" r="2.6" fill={POLARIS_CYAN} />
    </svg>
  );
}

/** 横排组合标：图形标 + 字标。 */
export function PolarisLockup({ markSize = 30 }: { markSize?: number }) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: markSize * 0.26 }}>
      <PolarisMark size={markSize} />
      <PolarisWordmark height={markSize * 0.55} />
    </span>
  );
}
