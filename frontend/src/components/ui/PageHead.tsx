import type { ReactNode } from 'react';

export interface PageHeadProps {
  eyebrow: string;
  title: string;
  sub?: string;
  right?: ReactNode;
}

/** Eyebrow + 大标题 + 副标题的页头（调用方用 tr() 传入当前语言文案）。 */
export function PageHead({ eyebrow, title, sub, right }: PageHeadProps) {
  return (
    <div className="row" style={{ alignItems: 'flex-start', marginBottom: 26 }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="h-eyebrow">{eyebrow}</div>
        <h1 className="h-title">{title}</h1>
        {sub && <p className="h-sub">{sub}</p>}
      </div>
      {right && (
        <div className="row gap10" style={{ flexShrink: 0, marginLeft: 16 }}>
          {right}
        </div>
      )}
    </div>
  );
}
