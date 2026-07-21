import type { ExperimentBudget, ExperimentStatus, HypothesisStatus } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   Experiment Lab 共享小件：假设 chip、状态进度、预算文案。
   ============================================================ */

/** 假设状态 chip（色点 + 文案）：testing 灰 / verified 绿 / falsified 红。
    title 传判定依据 evidence，鼠标悬停可见。 */
export function HypChip({ status, title }: { status: HypothesisStatus | string; title?: string }) {
  const map: Record<string, [string, string, string]> = {
    verified: ['var(--ok-bg)', 'var(--ok-tx)', tr('✓ 已验证', '✓ Verified')],
    falsified: ['var(--danger-bg)', 'var(--danger-tx)', tr('✗ 已证伪', '✗ Falsified')],
    testing: ['var(--surface-3)', 'var(--text-3)', tr('◴ 测试中', '◴ Testing')],
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
  const llmStop = tr('AI 判断可以收尾', 'AI decided to wrap up');
  const noImprove = tr('连续 2 轮主指标无提升，自动停止', 'No metric gain for 2 runs in a row — auto stopped');
  const debugLimit = tr('修错次数用完（3 次）仍未跑通', 'Still failing after all 3 debug attempts');
  const hypResolved = tr('所有假设都有结论了', 'All hypotheses resolved');
  const map: Record<string, string> = {
    stop: llmStop,
    decision_stop: llmStop,
    llm_stop: llmStop,
    max_runs: tr('达到最大运行次数上限', 'Hit the max run count'),
    max_hours: tr('达到时间上限', 'Hit the time limit'),
    no_improve: noImprove,
    no_improvement: noImprove,
    no_improve_stop: noImprove,
    debug_limit: debugLimit,
    debug_limit_exceeded: debugLimit,
    hypotheses_resolved: hypResolved,
    all_hypotheses_resolved: hypResolved,
    cancelled: tr('被人工取消', 'Cancelled manually'),
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
