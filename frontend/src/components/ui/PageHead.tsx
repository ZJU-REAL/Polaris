import type { ReactNode } from 'react';

export interface PageHeadProps {
  eyebrow: string;
  title: string;
  sub?: string;
  en?: string;
  right?: ReactNode;
}

/** Eyebrow + 大标题 + 中英副标题的页头。 */
export function PageHead({ eyebrow, title, sub, en, right }: PageHeadProps) {
  return (
    <div className="row" style={{ alignItems: 'flex-start', marginBottom: 26 }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="h-eyebrow">{eyebrow}</div>
        <h1 className="h-title">{title}</h1>
        {sub && (
          <p className="h-sub">
            {sub}
            {en && <span className="en"> · {en}</span>}
          </p>
        )}
      </div>
      {right && (
        <div className="row gap10" style={{ flexShrink: 0, marginLeft: 16 }}>
          {right}
        </div>
      )}
    </div>
  );
}
