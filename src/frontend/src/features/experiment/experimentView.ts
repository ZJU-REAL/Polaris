import type { ExperimentDetail, ExperimentRead, ExperimentStatus, RunnerBackendSummary } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   实验页（列表 + 详情 + 新建）共用的几样纯计算：哪些运行方式真的能用、
   一个实验现在的状态怎么用一句话说、做完之后的结果怎么概括。
   ============================================================ */

/**
 * 真的跑得起来的运行方式。
 *
 * 引擎目前只把「通过 SSH 在远程机器上运行」这条路接通了；清单里那些不要 SSH 的
 * （fmu / ngspice / openfoam）选了以后一定会在运行时报错。所以这里只留要 SSH 的。
 */
export function usableBackends(backends: RunnerBackendSummary[]): RunnerBackendSummary[] {
  return backends.filter((b) => b.credential_kinds.includes('ssh'));
}

/** 状态点的色调。 */
export function experimentTone(status: ExperimentStatus | string): 'active' | 'warn' | 'err' | 'ok' | 'idle' {
  switch (status) {
    case 'awaiting_gate':
    case 'waiting_user':
      return 'warn';
    case 'failed':
      return 'err';
    case 'done':
      return 'ok';
    case 'cancelled':
      return 'idle';
    default:
      return 'active';
  }
}

/** 一句话：这个实验现在在做什么 / 在等你什么 / 结果如何。 */
export function experimentSentence(status: ExperimentStatus | string): string {
  switch (status) {
    case 'planning':
      return tr('正在写实验计划', 'Writing the experiment plan');
    case 'awaiting_gate':
      return tr('等你确认计划，确认后开始运行', 'Waiting for you to confirm the plan, then it runs');
    case 'setup':
      return tr('正在搭建环境并写代码', 'Setting up and writing the code');
    case 'running':
      return tr('正在运行', 'Running');
    case 'waiting_user':
      return tr('等你回答一个问题，回答后继续', 'Waiting for your answer, then it continues');
    case 'reporting':
      return tr('正在写报告', 'Writing the report');
    case 'done':
      return tr('已完成', 'Done');
    case 'failed':
      return tr('没有完成', 'Didn’t finish');
    case 'cancelled':
      return tr('已取消', 'Cancelled');
    default:
      return String(status);
  }
}

/** 等你处理的（提问 / 确认预算）。 */
export function needsYou(exp: Pick<ExperimentRead, 'status'>): boolean {
  return exp.status === 'waiting_user' || exp.status === 'awaiting_gate';
}

export function fmtMetric(v: number): string {
  const a = Math.abs(v);
  if (a >= 1000) return v.toFixed(0);
  if (a >= 100) return v.toFixed(1);
  if (a >= 1) return v.toFixed(3).replace(/\.?0+$/, '');
  return v.toFixed(4).replace(/0+$/, '').replace(/\.$/, '');
}

export type HypothesisVerdict = 'verified' | 'falsified' | 'testing';

/** 假设的结论。引擎写 verified / falsified，旧数据里也有 supported / refuted。 */
export function hypothesisVerdict(status: string | null | undefined): HypothesisVerdict {
  if (status === 'verified' || status === 'supported') return 'verified';
  if (status === 'falsified' || status === 'refuted') return 'falsified';
  return 'testing';
}

export interface ExperimentSummary {
  /** 主指标名；计划里没定为 null */
  metric: string | null;
  direction: 'maximize' | 'minimize';
  /** 最好的一轮 */
  best: { round: number; value: number } | null;
  rounds: { total: number; succeeded: number; failed: number };
  hypotheses: { total: number; verified: number; falsified: number; open: number };
}

/** 做完（或做到一半）的实验，结果的几个数。 */
export function summarize(exp: Pick<ExperimentDetail, 'plan' | 'runs'>): ExperimentSummary {
  const direction = exp.plan?.primary_metric?.direction === 'minimize' ? 'minimize' : 'maximize';
  const runs = exp.runs ?? [];
  let best: ExperimentSummary['best'] = null;
  for (const r of runs) {
    const v = r.primary_value;
    if (typeof v !== 'number' || !Number.isFinite(v)) continue;
    if (!best || (direction === 'minimize' ? v < best.value : v > best.value)) best = { round: r.seq, value: v };
  }
  const hyps = exp.plan?.hypotheses ?? [];
  const verdicts = hyps.map((h) => hypothesisVerdict(h.status));
  const okStatuses = new Set(['succeeded', 'done']);
  return {
    metric: exp.plan?.primary_metric?.name ?? null,
    direction,
    best,
    rounds: {
      total: runs.length,
      succeeded: runs.filter((r) => okStatuses.has(r.status)).length,
      failed: runs.filter((r) => r.status === 'failed').length,
    },
    hypotheses: {
      total: hyps.length,
      verified: verdicts.filter((v) => v === 'verified').length,
      falsified: verdicts.filter((v) => v === 'falsified').length,
      open: verdicts.filter((v) => v === 'testing').length,
    },
  };
}

/** 结果的一句话（页头状态句下面那行）；没什么可说的返回 null。 */
export function summaryLine(s: ExperimentSummary): string | null {
  const parts: string[] = [];
  if (s.best) {
    const name = s.metric ?? tr('主指标', 'primary metric');
    parts.push(tr(`${name} 最好 ${fmtMetric(s.best.value)}（第 ${s.best.round} 轮）`, `Best ${name} ${fmtMetric(s.best.value)} (round ${s.best.round})`));
  }
  if (s.rounds.total > 0) {
    parts.push(
      s.rounds.failed > 0
        ? tr(`跑了 ${s.rounds.total} 轮，${s.rounds.failed} 轮失败`, `${s.rounds.total} ${s.rounds.total === 1 ? 'round' : 'rounds'}, ${s.rounds.failed} failed`)
        : tr(`跑了 ${s.rounds.total} 轮`, `${s.rounds.total} ${s.rounds.total === 1 ? 'round' : 'rounds'}`),
    );
  }
  if (s.hypotheses.total > 0) {
    const h = s.hypotheses;
    const bits = [
      h.verified > 0 ? tr(`${h.verified} 条成立`, `${h.verified} held`) : '',
      h.falsified > 0 ? tr(`${h.falsified} 条不成立`, `${h.falsified} didn’t hold`) : '',
      h.open > 0 ? tr(`${h.open} 条没有结论`, `${h.open} undecided`) : '',
    ].filter(Boolean);
    parts.push(tr(`${h.total} 条假设：${bits.join('，')}`, `${h.total} ${h.total === 1 ? 'hypothesis' : 'hypotheses'}: ${bits.join(', ')}`));
  }
  return parts.length > 0 ? parts.join(' · ') : null;
}
