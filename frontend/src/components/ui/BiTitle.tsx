export interface BiTitleProps {
  zh: string;
  en?: string;
  size?: number;
}

/** 中英双语标题：中文主标题 + 英文副标题。 */
export function BiTitle({ zh, en, size = 15 }: BiTitleProps) {
  return (
    <div>
      <div style={{ fontSize: size, fontWeight: 650, color: 'var(--text)', letterSpacing: '-0.01em', lineHeight: 1.3 }}>
        {zh}
      </div>
      {en && <div style={{ fontSize: size - 2.5, color: 'var(--text-3)', marginTop: 2, lineHeight: 1.3 }}>{en}</div>}
    </div>
  );
}
