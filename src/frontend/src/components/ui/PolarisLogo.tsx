/* ============================================================
   Polaris 品牌标识（据 polaris.svg / polaris_lockup.svg 复刻）：
   - PolarisMark：蓝色主体（A/i 合体，内部镂空）+ 右上角青绿圆点（图形标）。
     圆点可关（dot={false}）——Buddy 用「有没有那一点」表示思考中/已答完。
   - PolarisWordmark：几何方圆风格的 Polaris 字标（蓝色描边 + 青绿点缀）。
   品牌色：蓝 #126DFF / 青绿 #19D3A7。
   ============================================================ */

export const POLARIS_BLUE = '#126DFF';
export const POLARIS_TEAL = '#19D3A7';

/** 图形标（正方形）；size 为渲染像素。

    ``dot`` 控制右上角那个绿点。Buddy 拿它当状态：**思考/作答中只画蓝色主体，答完
    才把点补上**——一个「还差一笔」的标记比转圈更贴切，而且它就是这套标识本身，
    不必再造一个 loading 图形。 */
export function PolarisMark({
  size = 26,
  title = 'Polaris',
  dot = true,
}: {
  size?: number;
  title?: string;
  dot?: boolean;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 38 38"
      fill="none"
      role="img"
      aria-label={title}
      style={{ display: 'block', flexShrink: 0 }}
    >
      {/* 蓝色主体（A/i 合体）。evenodd 让内部两个三角镂空，透出背景 */}
      <path
        fillRule="evenodd"
        clipRule="evenodd"
        d="M14.5888 7.77765C16.2401 7.14973 18.1314 7.84717 18.8512 9.33629L18.8813 9.40059L23.3166 19.1746L27.4553 17.3081C28.7126 16.7409 30.2298 17.1971 30.8786 18.3257L30.9103 18.3827C31.204 18.9283 31.256 19.5557 31.0576 20.1341L31.0315 20.2062L28.3231 27.2981C27.8528 28.5294 26.381 29.1787 25.0358 28.7482C24.3677 28.5344 23.826 28.0792 23.5357 27.4906L23.5045 27.4247L21.6269 23.288L8.74766 29.098C7.98987 29.4398 7.11299 29.4778 6.32588 29.2067L6.2419 29.1765L6.20584 29.1625L6.10859 29.1229C4.60563 28.4799 3.94394 26.8582 4.60616 25.4749L4.63717 25.4122L12.8735 9.28094C13.208 8.62579 13.7888 8.10382 14.5083 7.80942L14.5888 7.77765ZM16.1618 10.3778L8.30761 26.1502L20.6214 20.4547L16.1618 10.3778ZM27.6579 20.4883L25.0192 21.7538L26.2052 24.5325L27.6579 20.4883Z"
        fill={POLARIS_BLUE}
      />
      {/* 两笔交叠处的深色高光 */}
      <path
        opacity="0.45"
        d="M20.5686 20.3856L23.5069 19.054L24.7636 21.836L21.8251 23.1674L20.5686 20.3856Z"
        fill="#0052D9"
      />
      {/* 绿色圆点（i 上的点）：状态位，见上面注释 */}
      {dot && <circle cx="30.961" cy="14.23" r="1.921" fill={POLARIS_TEAL} />}
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
      <g stroke={POLARIS_BLUE} strokeWidth="5" strokeLinejoin="round">
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
      {/* 填充轮廓另描 1px 同色边，配合加粗字重 */}
      <g fill={POLARIS_BLUE} stroke={POLARIS_BLUE} strokeWidth="1" strokeLinejoin="round">
        {/* s 端头斜切补片：右上端与左下端 */}
        <path d="M126.0 13.35 L128.7 17.35 H126.0 Z" />
        <path d="M114.4 31.7 L111.9 27.7 H114.4 Z" />
        {/* l：顶部斜切竖干 */}
        <path d="M52.9 5.6 L56.9 3.0 V31.7 H52.9 Z" />
        {/* a 上横臂 + 右干（填充轮廓：左端斜切、右下端斜切） */}
        <path d="M63.0 11.7 H74.9 C79.2 11.7 80.6 13.9 80.6 19.2 V31.7 H76.6 V18.8 C76.6 16.4 75.4 15.7 73.4 15.7 H65.4 Z" />
      </g>
      {/* 青绿细节：P 碗内短横 + i 点 */}
      <rect x="9.9" y="11.4" width="9" height="2.4" rx="1.2" fill={POLARIS_TEAL} />
      <circle cx="105.4" cy="5.2" r="2.6" fill={POLARIS_TEAL} />
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
