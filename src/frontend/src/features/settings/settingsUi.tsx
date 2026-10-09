/* ============================================================
   设置页的排版积木（#857）：分区标签在卡片外，卡片里是一行行设置。

   以前每一块都是「卡片 + 图标 + 标题 + 一段说明 + 表单」，十几块叠在一起，
   图标和标题比设置本身还显眼；两栏并排又让视线来回跳。现在统一成
   Section（灰色小标签 + 可选操作）→ Group（一张平卡片）→ Row（左名称右控件）。
   ============================================================ */
import type { ReactNode } from 'react';
import './settings.css';

export function SettingsStack({ children, wide }: { children: ReactNode; wide?: boolean }) {
  return <div className={'st-page' + (wide ? ' st-page-wide' : '')}>{children}</div>;
}

export function SettingsSection({
  title,
  desc,
  actions,
  children,
  id,
}: {
  title: ReactNode;
  /** 一句话说明，放在标签下、卡片上 */
  desc?: ReactNode;
  /** 标签右侧的操作（如「添加」「刷新」） */
  actions?: ReactNode;
  children: ReactNode;
  id?: string;
}) {
  return (
    <section className="st-section" id={id}>
      <div className="st-section-head">
        <h3 className="st-section-label">{title}</h3>
        {actions && <div className="st-section-actions">{actions}</div>}
      </div>
      {desc && <p className="st-section-desc">{desc}</p>}
      {children}
    </section>
  );
}

export function SettingsGroup({ children, pad }: { children: ReactNode; pad?: boolean }) {
  return <div className={'st-group' + (pad ? ' st-group-pad' : '')}>{children}</div>;
}

export function SettingsRow({
  label,
  hint,
  error,
  children,
  stack,
  labelId,
}: {
  label: ReactNode;
  /** 一行说明；更长的解释放文档 */
  hint?: ReactNode;
  error?: ReactNode;
  children?: ReactNode;
  /** 控件很宽（多行输入、标签输入）时，名称在上、控件占满整行 */
  stack?: boolean;
  labelId?: string;
}) {
  return (
    <div className={'st-row' + (stack ? ' st-row-stack' : '')}>
      <div className="st-row-text">
        <div className="st-row-label" id={labelId}>{label}</div>
        {hint && !error && <div className="st-row-hint">{hint}</div>}
        {error && <div className="st-row-error">{error}</div>}
      </div>
      {children !== undefined && <div className="st-row-control">{children}</div>}
    </div>
  );
}

export function SettingsActions({ note, children }: { note?: ReactNode; children: ReactNode }) {
  return (
    <div className="st-actions">
      {note && <span className="st-actions-note">{note}</span>}
      {children}
    </div>
  );
}

export type DotTone = 'ok' | 'warn' | 'err' | 'idle' | 'busy';

/** 状态：8px 圆点 + 一个词。 */
export function StatusDot({ tone, children, title }: { tone: DotTone; children: ReactNode; title?: string }) {
  return (
    <span className="st-status" title={title}>
      <span className={`st-dot st-dot-${tone}`} aria-hidden />
      {children}
    </span>
  );
}
