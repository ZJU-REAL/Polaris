import type { ExperimentBudget, ExperimentStatus, HypothesisStatus } from '../../lib/api';

/* ============================================================
   Experiment Lab 共享小件：假设 chip、状态进度、预算文案。
   ============================================================ */

/** 假设状态 chip（色点 + 文案）：testing 灰 / verified 绿 / falsified 红。
    title 传判定依据 evidence，鼠标悬停可见。 */
export function HypChip({ status, title }: { status: HypothesisStatus | string; title?: string }) {
  const map: Record<string, [string, string, string]> = {
    verified: ['var(--ok-bg)', 'var(--ok-tx)', '✓ 已验证'],
    falsified: ['var(--danger-bg)', 'var(--danger-tx)', '✗ 已证伪'],
    testing: ['var(--surface-3)', 'var(--text-3)', '◴ 测试中'],
  };
  const [bg, c, t] = map[status] ?? map.testing!;
  return (
    <span className="pill sm" title={title} style={{ background: bg, color: c, flexShrink: 0 }}>
      {t}
    </span>
  );
}

/** 迭代停止原因 → 大白话（未知值原样显示）。 */
export function stopReasonText(reason: string | null | undefined): string | null {
  if (!reason) return null;
  const map: Record<string, string> = {
    stop: 'AI 判断可以收尾',
    decision_stop: 'AI 判断可以收尾',
    llm_stop: 'AI 判断可以收尾',
    max_runs: '达到最大运行次数上限',
    max_hours: '达到时间上限',
    no_improve: '连续 2 轮主指标无提升，自动停止',
    no_improvement: '连续 2 轮主指标无提升，自动停止',
    no_improve_stop: '连续 2 轮主指标无提升，自动停止',
    debug_limit: '修错次数用完（3 次）仍未跑通',
    debug_limit_exceeded: '修错次数用完（3 次）仍未跑通',
    hypotheses_resolved: '所有假设都有结论了',
    all_hypotheses_resolved: '所有假设都有结论了',
    cancelled: '被人工取消',
  };
  return map[reason] ?? reason;
}

/** 实验主流程阶段（终态 failed/cancelled 不在其中）。 */
export const EXP_FLOW: { key: ExperimentStatus; zh: string }[] = [
  { key: 'planning', zh: '计划' },
  { key: 'awaiting_gate', zh: '预算审批' },
  { key: 'setup', zh: '建环境' },
  { key: 'running', zh: '运行' },
  { key: 'reporting', zh: '报告' },
  { key: 'done', zh: '完成' },
];

/** 状态 → 进度百分比（列表卡进度条用）。 */
export function expProgress(status: ExperimentStatus): number {
  const i = EXP_FLOW.findIndex((s) => s.key === status);
  if (i >= 0) return Math.round((i / (EXP_FLOW.length - 1)) * 100);
  return 100; // failed / cancelled：走到哪算哪，统一画满并靠颜色区分
}

/** 预算 → "≤4h · ≤10 runs"。 */
export function budgetText(budget: ExperimentBudget | null | undefined): string {
  if (!budget) return '—';
  const parts: string[] = [];
  if (budget.max_hours !== undefined) parts.push(`≤${budget.max_hours}h`);
  if (budget.max_runs !== undefined) parts.push(`≤${budget.max_runs} runs`);
  return parts.length > 0 ? parts.join(' · ') : '—';
}
