import type { CSSProperties } from 'react';
import { tr } from '../../lib/i18n';
import type { FeedbackSeverity, FeedbackStatus, FeedbackType } from '../../lib/api';

/* ============================================================
   反馈枚举的中英文案与徽章样式。
   注意：文案不在模块顶层 tr（那会在切换语言时不更新），
   而是保留 {zh,en} 常量，在下面的函数/组件里渲染时才 tr。
   ============================================================ */

const TYPE_LABELS: Record<FeedbackType, { zh: string; en: string }> = {
  bug: { zh: '缺陷', en: 'Bug' },
  feature: { zh: '功能建议', en: 'Feature' },
  ui: { zh: '界面体验', en: 'UI' },
  question: { zh: '使用疑问', en: 'Question' },
  perf: { zh: '性能', en: 'Performance' },
  task: { zh: '任务', en: 'Task' },
  other: { zh: '其他', en: 'Other' },
};

const SEVERITY_LABELS: Record<FeedbackSeverity, { zh: string; en: string }> = {
  blocker: { zh: '阻塞', en: 'Blocker' },
  high: { zh: '严重', en: 'High' },
  normal: { zh: '一般', en: 'Normal' },
  low: { zh: '轻微', en: 'Low' },
};

const STATUS_LABELS: Record<FeedbackStatus, { zh: string; en: string }> = {
  new: { zh: '新提交', en: 'New' },
  triaged: { zh: '已分诊', en: 'Triaged' },
  in_progress: { zh: '处理中', en: 'In progress' },
  resolved: { zh: '已解决', en: 'Resolved' },
  closed: { zh: '已关闭', en: 'Closed' },
  wontfix: { zh: '不予处理', en: "Won't fix" },
};

export function typeLabel(t: FeedbackType): string {
  const m = TYPE_LABELS[t];
  return m ? tr(m.zh, m.en) : t;
}
export function severityLabel(s: FeedbackSeverity): string {
  const m = SEVERITY_LABELS[s];
  return m ? tr(m.zh, m.en) : s;
}
export function statusLabel(s: FeedbackStatus): string {
  const m = STATUS_LABELS[s];
  return m ? tr(m.zh, m.en) : s;
}

/** 提交入口可选类型（不含 task —— 那是管理内部用的）。 */
export const WIDGET_TYPES: FeedbackType[] = ['bug', 'feature', 'ui', 'question', 'perf', 'other'];
/** 全部类型（管理端过滤用）。 */
export const ALL_TYPES: FeedbackType[] = ['bug', 'feature', 'ui', 'question', 'perf', 'task', 'other'];
export const SEVERITIES: FeedbackSeverity[] = ['blocker', 'high', 'normal', 'low'];
export const STATUSES: FeedbackStatus[] = ['new', 'triaged', 'in_progress', 'resolved', 'closed', 'wontfix'];

const SEVERITY_STYLE: Record<FeedbackSeverity, CSSProperties> = {
  blocker: { background: 'var(--danger-bg)', color: 'var(--danger-tx)' },
  high: { background: 'var(--warn-bg)', color: 'var(--warn-tx)' },
  normal: { background: 'var(--surface-3)', color: 'var(--text-2)' },
  low: { background: 'var(--surface-3)', color: 'var(--text-3)' },
};

const STATUS_STYLE: Record<FeedbackStatus, CSSProperties> = {
  new: { background: 'var(--accent-soft)', color: 'var(--accent-text)' },
  triaged: { background: 'var(--surface-3)', color: 'var(--text-2)' },
  in_progress: { background: 'var(--warn-bg)', color: 'var(--warn-tx)' },
  resolved: { background: 'var(--ok-bg)', color: 'var(--ok-tx)' },
  closed: { background: 'var(--surface-3)', color: 'var(--text-3)' },
  wontfix: { background: 'var(--surface-3)', color: 'var(--text-3)' },
};

export function TypePill({ type }: { type: FeedbackType }) {
  return (
    <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-2)' }}>
      {typeLabel(type)}
    </span>
  );
}

export function SeverityPill({ severity }: { severity: FeedbackSeverity }) {
  return (
    <span className="pill sm" style={SEVERITY_STYLE[severity]}>
      {severityLabel(severity)}
    </span>
  );
}

export function FeedbackStatusPill({ status }: { status: FeedbackStatus }) {
  return (
    <span className="pill sm" style={STATUS_STYLE[status]}>
      {statusLabel(status)}
    </span>
  );
}
