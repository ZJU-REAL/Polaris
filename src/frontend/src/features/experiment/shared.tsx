import type { ExperimentBudget, ExperimentStatus, HypothesisStatus } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { StatusDot, type DotTone } from '../voyages/shared/StatusDot';

/* ============================================================
   Experiment Lab 共享小件：假设 chip、状态进度、预算文案。
   ============================================================ */

/** 假设状态（色点 + 文案）：testing 灰 / verified 绿 / falsified 红。
    title 传判定依据 evidence，鼠标悬停可见。 */
export function HypChip({ status, title }: { status: HypothesisStatus | string; title?: string }) {
  const map: Record<string, [DotTone, string]> = {
    verified: ['ok', tr('已验证', 'Verified')],
    falsified: ['err', tr('已证伪', 'Refuted')],
    testing: ['idle', tr('验证中', 'Testing')],
  };
  const [tone, label] = map[status] ?? map.testing!;
  return <StatusDot tone={tone} label={label} title={title} />;
}

/** 迭代停止原因 → 大白话（未知值原样显示）。 */
export function stopReasonText(reason: string | null | undefined): string | null {
  if (!reason) return null;
  const llmStop = tr('AI 判断可以结束', 'The AI decided to stop');
  const noImprove = tr('连续 2 轮主指标无提升', 'No improvement in 2 rounds');
  const debugLimit = tr('修复 3 次后仍未跑通', 'Still failing after 3 fixes');
  const hypResolved = tr('假设都已有结论', 'All hypotheses answered');
  const map: Record<string, string> = {
    stop: llmStop,
    decision_stop: llmStop,
    llm_stop: llmStop,
    max_runs: tr('达到轮次上限', 'Round limit reached'),
    max_hours: tr('达到时长上限', 'Time limit reached'),
    no_improve: noImprove,
    no_improvement: noImprove,
    no_improve_stop: noImprove,
    debug_limit: debugLimit,
    debug_limit_exceeded: debugLimit,
    hypotheses_resolved: hypResolved,
    all_hypotheses_resolved: hypResolved,
    cancelled: tr('已手动取消', 'Cancelled'),
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

/** 预算 → "≤ 4 小时 · ≤ 10 轮" / "≤ 4 h · ≤ 10 rounds"。 */
export function budgetText(budget: ExperimentBudget | null | undefined): string {
  if (!budget) return '—';
  const parts: string[] = [];
  if (budget.max_hours) parts.push(tr(`≤ ${budget.max_hours} 小时`, `≤ ${budget.max_hours} h`));
  if (budget.max_runs) parts.push(tr(`≤ ${budget.max_runs} 轮`, `≤ ${budget.max_runs} rounds`));
  return parts.length > 0 ? parts.join(' · ') : tr('不限时', 'No limit');
}
