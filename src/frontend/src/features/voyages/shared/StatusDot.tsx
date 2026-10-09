import { tr } from '../../../lib/i18n';

/* ============================================================
   状态点 + 文字（8px 圆点 + 12px 文案，不用彩色胶囊）。
   任务详情 / 实验 / 想法页局部渲染的状态都用它；共享的 StatusPill
   若改成同样的点+字形态，这里可以直接换成它。
   ============================================================ */

export type DotTone = 'ok' | 'warn' | 'err' | 'idle' | 'active';

const DOT_COLOR: Record<DotTone, string> = {
  ok: 'var(--ok)',
  warn: 'var(--warn)',
  err: 'var(--danger)',
  idle: 'var(--muted-dot)',
  active: 'var(--accent)',
};

export function StatusDot({ tone, label, title }: { tone: DotTone; label: string; title?: string }) {
  return (
    <span
      title={title}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        fontSize: 12,
        color: 'var(--text-2)',
        flexShrink: 0,
        whiteSpace: 'nowrap',
      }}
    >
      <span style={{ width: 8, height: 8, borderRadius: '50%', background: DOT_COLOR[tone], flexShrink: 0 }} />
      {label}
    </span>
  );
}

/** 任务 / 步骤 / 实验状态 → 色调 + 文案（模块级只存 zh/en，渲染处再 tr）。 */
const TASK_STATUS: Record<string, { tone: DotTone; zh: string; en: string }> = {
  // 任务
  planning: { tone: 'active', zh: '规划中', en: 'Planning' },
  executing: { tone: 'active', zh: '执行中', en: 'Running' },
  verifying: { tone: 'active', zh: '校验中', en: 'Checking' },
  replanning: { tone: 'active', zh: '调整计划', en: 'Adjusting plan' },
  paused_gate: { tone: 'warn', zh: '等待审批', en: 'Needs approval' },
  paused_ask: { tone: 'warn', zh: '等你回复', en: 'Needs your reply' },
  paused_error: { tone: 'err', zh: '出错暂停', en: 'Paused on error' },
  done: { tone: 'ok', zh: '已完成', en: 'Done' },
  failed: { tone: 'err', zh: '失败', en: 'Failed' },
  cancelled: { tone: 'idle', zh: '已取消', en: 'Cancelled' },
  // 步骤 / 实验轮次
  pending: { tone: 'idle', zh: '待执行', en: 'Pending' },
  running: { tone: 'active', zh: '运行中', en: 'Running' },
  passed: { tone: 'ok', zh: '通过', en: 'Passed' },
  skipped: { tone: 'idle', zh: '已跳过', en: 'Skipped' },
  obsolete: { tone: 'idle', zh: '已作废', en: 'Dropped' },
  succeeded: { tone: 'ok', zh: '成功', en: 'Succeeded' },
  // 实验
  awaiting_gate: { tone: 'warn', zh: '等待审批', en: 'Needs approval' },
  setup: { tone: 'active', zh: '搭建环境', en: 'Setting up' },
  reporting: { tone: 'active', zh: '生成报告', en: 'Writing report' },
  waiting_user: { tone: 'warn', zh: '等你回复', en: 'Needs your reply' },
};

/** 任务 / 步骤 / 实验状态的点+字（未知状态原样显示、灰点）。 */
export function TaskStatus({ status, title }: { status: string; title?: string }) {
  const m = TASK_STATUS[status];
  return <StatusDot tone={m ? m.tone : 'idle'} label={m ? tr(m.zh, m.en) : status} title={title} />;
}
