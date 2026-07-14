import type { ExperimentBudget, ExperimentStatus, HypothesisStatus } from '../../lib/api';

/* ============================================================
   Experiment Lab 共享小件：假设 chip、状态进度、预算文案。
   ============================================================ */

/** 假设状态 chip（色点 + 文案）：testing / verified / falsified。 */
export function HypChip({ status }: { status: HypothesisStatus | string }) {
  const map: Record<string, [string, string, string]> = {
    verified: ['var(--ok-bg)', 'var(--ok-tx)', '✓ 已验证'],
    falsified: ['var(--danger-bg)', 'var(--danger-tx)', '✗ 已证伪'],
    testing: ['var(--warn-bg)', 'var(--warn-tx)', '◴ 测试中'],
  };
  const [bg, c, t] = map[status] ?? map.testing!;
  return (
    <span className="pill sm" style={{ background: bg, color: c, flexShrink: 0 }}>
      {t}
    </span>
  );
}

/** 实验主流程阶段（终态 failed/cancelled 不在其中）。 */
export const EXP_FLOW: { key: ExperimentStatus; zh: string }[] = [
  { key: 'planning', zh: '计划' },
  { key: 'awaiting_gate', zh: '预算闸门' },
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
