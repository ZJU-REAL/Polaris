import { tr } from '../../../lib/i18n';
import type { VoyagePlanEvent, VoyageStepRead } from '../../../lib/api';

/* ============================================================
   任务步骤的纯工具与数据变换（无 JSX）：排序、标记色、时间线插桩。
   从 VoyageDetailPage 抽出，供任务详情页与实验运行台共用。
   ============================================================ */

/** 步骤 token 数：后端存 {prompt_tokens, completion_tokens} 字典，历史数据可能是数字。 */
export function stepTokenCount(tokens: VoyageStepRead['tokens']): number | null {
  if (typeof tokens === 'number') return tokens;
  if (tokens && typeof tokens === 'object') {
    return (tokens.prompt_tokens ?? 0) + (tokens.completion_tokens ?? 0);
  }
  return null;
}

export function asObj(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
}

export function num(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : 0;
}

export function stepMarker(step: VoyageStepRead): { bg: string; color: string } {
  if (step.status === 'obsolete') return { bg: 'var(--surface-3)', color: 'var(--text-4)' };
  if (step.verdict && !step.verdict.passed) return { bg: 'var(--danger-bg)', color: 'var(--danger-tx)' };
  switch (step.status) {
    case 'passed':
      return { bg: 'var(--ok-bg)', color: 'var(--ok-tx)' };
    case 'running':
    case 'verifying':
      return { bg: 'var(--accent)', color: '#fff' };
    case 'failed':
      return { bg: 'var(--danger-bg)', color: 'var(--danger-tx)' };
    default:
      return { bg: 'var(--surface-2)', color: 'var(--text-3)' };
  }
}

/** 清单序 = 执行序：按 rank 排（计划调整的插入节点 rank 取间隙值），seq 只是创建序。 */
export function byListOrder(a: VoyageStepRead, b: VoyageStepRead): number {
  return (a.rank ?? 0) - (b.rank ?? 0) || a.seq - b.seq;
}

// —— 计划调整（plan_history）——

/** source → 大白话（模块级常量只存 zh/en，渲染处再 tr）。 */
export const PLAN_SOURCE: Record<string, { zh: string; en: string }> = {
  signal: { zh: '按执行结果自动调整', en: 'Auto-adjusted by results' },
  navigator: { zh: 'AI 调整计划', en: 'AI adjusted the plan' },
  template: { zh: '按预设分支调整', en: 'Preset branch adjustment' },
  budget: { zh: '预算用尽，跳过剩余步骤收尾', en: 'Budget spent — skipped remaining steps to wrap up' },
};

/** 作废步骤的原因：找作废它的那次计划调整（iteration 大于其创建轮次且最接近的一条）。 */
export function obsoleteReasonOf(step: VoyageStepRead, events: VoyagePlanEvent[]): string {
  const created = step.provenance?.plan_iteration ?? 0;
  const ev = events
    .filter((e) => e.iteration > created && e.obsoleted > 0)
    .sort((a, b) => a.iteration - b.iteration)[0];
  return ev?.reason
    ? tr(`已作废：${ev.reason}`, `Dropped: ${ev.reason}`)
    : tr('已作废：计划调整时被替换', 'Dropped: replaced during a plan adjustment');
}

// —— 时间线条目：步骤 + 计划调整分隔 ——

export type TimelineEntry =
  | { kind: 'step'; step: VoyageStepRead; index: number }
  | { kind: 'plan'; event: VoyagePlanEvent };

/** 按 plan_history 在第一个「该次调整新增」的步骤前插入分隔条目。 */
export function buildTimelineEntries(
  steps: VoyageStepRead[],
  events: VoyagePlanEvent[],
): TimelineEntry[] {
  const byIteration = new Map(events.map((e) => [e.iteration, e]));
  const inserted = new Set<number>();
  const entries: TimelineEntry[] = [];
  let index = 0;
  for (const step of steps) {
    const iter = step.provenance?.plan_iteration ?? 0;
    if (iter > 0 && !inserted.has(iter) && byIteration.has(iter)) {
      inserted.add(iter);
      entries.push({ kind: 'plan', event: byIteration.get(iter)! });
    }
    entries.push({ kind: 'step', step, index: ++index });
  }
  return entries;
}
