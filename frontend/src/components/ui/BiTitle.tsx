import { tr } from '../../lib/i18n';

export interface BiTitleProps {
  zh: string;
  en?: string;
  size?: number;
}

/** 标题：按当前语言单语显示（英文缺失时回退中文）。 */
export function BiTitle({ zh, en, size = 15 }: BiTitleProps) {
  return (
    <div>
      <div style={{ fontSize: size, fontWeight: 650, color: 'var(--text)', letterSpacing: '-0.01em', lineHeight: 1.3 }}>
        {tr(zh, en)}
      </div>
    </div>
  );
}
