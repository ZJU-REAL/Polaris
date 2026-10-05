import type {
  ExperimentDetail,
  ExperimentEvaluation,
  ExperimentMetricPoint,
  ExperimentRunRead,
  PrimaryMetric,
} from '../../lib/api';

export interface RunEvidence {
  run: ExperimentRunRead;
  kind: 'valid' | 'invalid' | 'unverified' | 'legacy';
  comparable: boolean;
  value: number | null;
  previousValue: number | null;
  reasons: string[];
  evaluation?: ExperimentEvaluation;
}

export interface ExperimentRunEvidence {
  versioned: boolean;
  selector: string;
  direction: PrimaryMetric['direction'];
  runs: RunEvidence[];
  primaryPoints: ExperimentMetricPoint[];
  best: ExperimentMetricPoint | null;
  allSeries: RunMetricSeries[];
}

export interface RunMetricSeries {
  name: string;
  runSeq: number;
  evaluationId: string | null;
  points: ExperimentMetricPoint[];
}

function finite(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function finalMetric(evaluation: ExperimentEvaluation, selector: string): number | null {
  const series = evaluation.metrics?.[selector];
  const value = Array.isArray(series) ? series[series.length - 1]?.value : null;
  return finite(value) ? value : null;
}

function metricPoints(metrics: Record<string, unknown> | null | undefined) {
  return Object.entries(metrics ?? {}).map(([name, series]) => ({
    name,
    points: Array.isArray(series) ? series.flatMap((point: unknown) => {
      if (!point || typeof point !== 'object') return [];
      const { step, value } = point as { step?: unknown; value?: unknown };
      return finite(step) && finite(value) ? [{ step, value }] : [];
    }) : [],
  }));
}

/** Use host-accepted evidence, never a failed run's attractive diagnostic score. */
export function selectRunEvidence(exp: Pick<ExperimentDetail, 'runs' | 'plan' | 'iteration_state'>): ExperimentRunEvidence {
  const state = exp.iteration_state;
  const versioned = !!state && (
    'research_contract' in state || 'evaluations' in state || 'incumbent' in state || 'result_bundle' in state
  );
  const bundle = state?.result_bundle;
  const contract = state?.research_contract ?? bundle?.research_contract;
  const primary = contract?.protocol.primary_metric ?? exp.plan?.primary_metric;
  const selector = primary?.selector ?? primary?.name ?? '';
  const direction = primary?.direction === 'minimize' ? 'minimize' : 'maximize';
  const contractId = contract?.contract_id ?? bundle?.contract_id;
  const protocolId = contract?.protocol_id ?? bundle?.protocol_id;
  const bundleMatches = !!bundle && !!contractId && !!protocolId
    && bundle.contract_id === contractId && bundle.protocol_id === protocolId;
  const evaluations = state?.evaluations ?? (bundleMatches ? [
    ...(bundle?.valid_evaluations ?? []), ...(bundle?.invalid_evaluations ?? []),
  ] : []);
  const byRun = new Map<string, ExperimentEvaluation[]>();
  for (const evaluation of evaluations) {
    byRun.set(evaluation.run_id, [...(byRun.get(evaluation.run_id) ?? []), evaluation]);
  }
  let previousValue: number | null = null;
  const runs: RunEvidence[] = [...exp.runs].sort((a, b) => a.seq - b.seq).map((run) => {
    const value = finite(run.primary_value) ? run.primary_value : null;
    const matching = byRun.get(run.id) ?? [];
    const evaluation = matching.length === 1 ? matching[0] : undefined;
    const reasons = [...(evaluation?.invalid_reasons ?? [])];
    let comparable = false;
    let kind: RunEvidence['kind'] = 'unverified';
    if (!versioned) {
      comparable = run.status === 'succeeded' && run.exit_code === 0 && value !== null;
      kind = comparable ? 'legacy' : run.status === 'failed' ? 'invalid' : 'unverified';
      if (run.status === 'failed') reasons.push('run_not_successful');
    } else if (evaluation) {
      if (!contractId || !protocolId || evaluation.contract_id !== contractId
        || evaluation.protocol_id !== protocolId) reasons.push('protocol_identity_mismatch');
      if (evaluation.evaluation_id !== `run:${run.id}` || evaluation.seq !== run.seq
        || !evaluation.candidate_id) reasons.push('evaluation_identity_mismatch');
      if (run.status !== 'succeeded' || run.exit_code !== 0
        || evaluation.status !== 'succeeded' || evaluation.exit_code !== 0) reasons.push('run_not_successful');
      if (value === null || !finite(evaluation.primary_value)
        || finalMetric(evaluation, selector) !== evaluation.primary_value
        || evaluation.primary_value !== value) reasons.push('primary_metric_value_mismatch');
      comparable = evaluation.valid === true && reasons.length === 0;
      kind = comparable ? 'valid' : 'invalid';
    } else if (matching.length > 1) {
      reasons.push('ambiguous_evaluation_identity');
      kind = 'invalid';
    }
    const entry: RunEvidence = {
      run, kind, comparable, value, previousValue: comparable ? previousValue : null,
      reasons: [...new Set(reasons)], evaluation,
    };
    if (comparable) previousValue = value;
    return entry;
  });
  const comparable = runs.filter((entry) => entry.comparable && entry.value !== null);
  const primaryPoints = comparable.map((entry) => ({ step: entry.run.seq, value: entry.value as number }));
  let best: ExperimentMetricPoint | null = null;
  if (!versioned) {
    best = primaryPoints.reduce<ExperimentMetricPoint | null>((selected, point) => (
      selected === null || (direction === 'minimize' ? point.value < selected.value : point.value > selected.value)
        ? point : selected
    ), null);
  } else if (state?.incumbent) {
    const incumbent = state.incumbent;
    const selected = comparable.find((entry) => entry.evaluation?.evaluation_id === incumbent.evaluation_id
      && entry.evaluation.candidate_id === incumbent.candidate_id
      && incumbent.contract_id === contractId && incumbent.protocol_id === protocolId
      && entry.value === incumbent.primary_value);
    if (selected) best = { step: selected.run.seq, value: selected.value as number };
  } else if (bundleMatches && bundle.selected_candidate) {
    const selectedIds = new Set((bundle.selected_evaluations ?? []).map((evaluation) => evaluation.evaluation_id));
    const selected = comparable.find((entry) => !!entry.evaluation
      && entry.evaluation.candidate_id === bundle.selected_candidate?.candidate_id
      && selectedIds.has(entry.evaluation.evaluation_id));
    if (selected) best = { step: selected.run.seq, value: selected.value as number };
  }
  const allSeries: RunMetricSeries[] = [];
  for (const entry of comparable) {
    for (const series of metricPoints(versioned ? entry.evaluation?.metrics : entry.run.metrics)) {
      if (series.points.length > 0) allSeries.push({
        ...series, runSeq: entry.run.seq, evaluationId: entry.evaluation?.evaluation_id ?? null,
      });
    }
  }
  return {
    versioned, selector, direction, runs, primaryPoints, best,
    allSeries,
  };
}

/** Keep units separate and bound the default view without discarding history. */
export function selectMetricTraces(evidence: ExperimentRunEvidence, metric: string, showAll = false): RunMetricSeries[] {
  const traces = evidence.allSeries.filter((series) => series.name === metric);
  if (showAll || traces.length <= 6) return traces;
  const visibleRuns = new Set(traces.slice(-5).map((series) => series.runSeq));
  if (evidence.best) visibleRuns.add(evidence.best.step);
  return traces.filter((series) => visibleRuns.has(series.runSeq));
}
