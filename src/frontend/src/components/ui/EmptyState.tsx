import type { ReactNode } from 'react';
import { Icon, type IconName } from './Icon';
import { tr } from '../../lib/i18n';

/* ============================================================
   列表 / 面板的三种状态，全站共用一套外观：
   - EmptyState：48px 图标块 + 一行字（可选一行补充）+ 至多一个按钮
   - LoadingState：几行骨架（与真实行同高），不再居中写「加载中…」
   - ErrorState：一句「什么没成功 + 怎么办」+ 可选原始信息 + 重试
   样式在 global.css 的 .empty-state / .skel-rows / .empty.is-error。
   ============================================================ */

export interface EmptyStateProps {
  icon?: IconName;
  /** 一行：这里会出现什么，如「还没有文献库」。 */
  title: string;
  /** 可选的一行补充；能省就省。 */
  desc?: string;
  /** 至多一个按钮。 */
  action?: ReactNode;
  /** 紧凑模式（列表内嵌）。 */
  compact?: boolean;
}

/** 空状态。 */
export function EmptyState({ icon = 'book', title, desc, action, compact }: EmptyStateProps) {
  return (
    <div className={'empty-state' + (compact ? ' compact' : '')}>
      <div className="empty-state-icon">
        <Icon name={icon} size={20} />
      </div>
      <div className="empty-state-title">{title}</div>
      {desc && <div className="empty-state-desc">{desc}</div>}
      {action && <div className="empty-state-action">{action}</div>}
    </div>
  );
}

export interface LoadingStateProps {
  /** 读屏用的说明，默认「加载中…」；如「正在导入…」。 */
  label?: string;
  compact?: boolean;
  /** 骨架行数，默认 3。 */
  rows?: number;
}

/** 列表/面板的加载态：几行骨架，屏幕阅读器读 label。 */
export function LoadingState({ label, compact, rows = 3 }: LoadingStateProps) {
  return (
    <div
      className={'skel-rows' + (compact ? ' compact' : '')}
      role="status"
      aria-live="polite"
      style={{ padding: compact ? '4px 0' : '8px 0' }}
    >
      <span className="sr-only">{label ?? tr('加载中…', 'Loading…')}</span>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="skel" style={{ width: i === rows - 1 ? '62%' : '100%' }} />
      ))}
    </div>
  );
}

export interface ErrorStateProps {
  /** 什么没成功 + 怎么办，如「无法加载审批，请确认本机引擎正在运行」。 */
  title: string;
  /** 原始错误信息：等宽小字放在第二行，给需要排查的人看。 */
  detail?: string | null;
  /** 传了就显示「重试」按钮。 */
  onRetry?: () => void;
  /** 额外动作（如「去设置」），放在重试旁边。 */
  action?: ReactNode;
  compact?: boolean;
}

/** 列表/面板的加载失败态：一句话 + 可选原始信息 + 重试。 */
export function ErrorState({ title, detail, onRetry, action, compact }: ErrorStateProps) {
  return (
    <div className="empty is-error" role="alert" style={compact ? { padding: 16 } : undefined}>
      <div className="state-title">{title}</div>
      {detail && <div className="state-detail">{detail}</div>}
      {(onRetry || action) && (
        <div className="state-action">
          {onRetry && (
            <button className="btn btn-ghost sm" onClick={onRetry}>
              {tr('重试', 'Retry')}
            </button>
          )}
          {action}
        </div>
      )}
    </div>
  );
}
