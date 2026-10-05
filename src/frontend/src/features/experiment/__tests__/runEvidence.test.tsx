import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it } from 'vitest';
import type {
  ExperimentDetail,
  ExperimentEvaluation,
  ExperimentIncumbent,
  ExperimentResearchContract,
  ExperimentRunRead,
} from '../../../lib/api';
import { setLang } from '../../../lib/i18n';
import { RunTab } from '../RunTab';
import { selectMetricTraces, selectRunEvidence } from '../runEvidence';

const contract: ExperimentResearchContract = {
  contract_id: 'contract:current', protocol_id: 'protocol:current',
  protocol: { primary_metric: { name: 'accuracy', direction: 'maximize', selector: 'accuracy' } },
};

function run(seq: number, value: number | null, overrides: Partial<ExperimentRunRead> = {}): ExperimentRunRead {
  return {
    id: `run-${seq}`, seq, command: 'bash run.sh', status: 'succeeded', exit_code: 0,
    log_path: null, metrics: { accuracy: [{ step: 1, value }] },
    started_at: null, finished_at: null, primary_value: value, ...overrides,
  };
}

function evaluation(source: ExperimentRunRead, overrides: Partial<ExperimentEvaluation> = {}): ExperimentEvaluation {
  return {
    evaluation_id: `run:${source.id}`, run_id: source.id, seq: source.seq,
    candidate_id: `candidate:${source.seq}`, contract_id: contract.contract_id,
    protocol_id: contract.protocol_id, status: source.status, exit_code: source.exit_code,
    primary_value: source.primary_value ?? null, valid: true, invalid_reasons: [],
    metrics: { accuracy: [{ step: 1, value: source.primary_value ?? null }] }, ...overrides,
  };
}

function incumbent(selected: ExperimentEvaluation): ExperimentIncumbent {
  return {
    evaluation_id: selected.evaluation_id, candidate_id: selected.candidate_id,
    contract_id: selected.contract_id, protocol_id: selected.protocol_id,
    primary_value: selected.primary_value,
  };
}

function experiment(runs: ExperimentRunRead[], evaluations: ExperimentEvaluation[]): ExperimentDetail {
  return {
    id: 'experiment', project_id: 'project', idea_id: 'idea', idea_title: 'Test research',
    status: 'done', voyage_id: null, workdir: null, server_host: null, budget: null,
    created_at: '', updated_at: '', report: null, runs, metrics: null,
    plan: { primary_metric: { name: 'accuracy', direction: 'maximize' } },
    iteration_state: { research_contract: contract, evaluations },
  };
}

afterEach(() => setLang('zh'));

describe('experiment run evidence selection', () => {
  it('excludes failed .99 from best, trends, curves, and the next comparison', () => {
    const first = run(1, 0.6);
    const failed = run(2, 0.99, { status: 'failed', exit_code: 1 });
    const third = run(3, 0.61);
    const good = evaluation(first);
    const exp = experiment([first, failed, third], [
      good, evaluation(failed, { valid: false, invalid_reasons: ['run_not_successful'] }), evaluation(third),
    ]);
    exp.iteration_state!.incumbent = incumbent(good);
    const result = selectRunEvidence(exp);
    expect(result.best).toEqual({ step: 1, value: 0.6 });
    expect(result.primaryPoints).toEqual([{ step: 1, value: 0.6 }, { step: 3, value: 0.61 }]);
    expect(result.runs[1]).toMatchObject({ kind: 'invalid', value: 0.99, previousValue: null });
    expect(result.runs[2]?.previousValue).toBe(0.6);
    expect(result.allSeries.flatMap((series) => series.points.map((point) => point.value))).toEqual([0.6, 0.61]);
  });

  it('keeps attractive source-changed scores only as invalid diagnostics', () => {
    const source = run(1, 0.99);
    const exp = experiment([source], [evaluation(source, {
      valid: false, invalid_reasons: ['runtime_source_changed:train.py'],
    })]);
    const result = selectRunEvidence(exp);
    expect(result.best).toBeNull();
    expect(result.primaryPoints).toEqual([]);
    expect(result.allSeries).toEqual([]);
    expect(result.runs[0]).toMatchObject({ comparable: false, value: 0.99, kind: 'invalid' });
  });

  it('rejects a cached valid evaluation from a different frozen protocol', () => {
    const source = run(1, 0.99);
    const foreign = evaluation(source, { protocol_id: 'protocol:old' });
    const exp = experiment([source], [foreign]);
    exp.iteration_state!.incumbent = incumbent(foreign);
    const result = selectRunEvidence(exp);
    expect(result.best).toBeNull();
    expect(result.primaryPoints).toEqual([]);
    expect(result.runs[0]?.reasons).toContain('protocol_identity_mismatch');
  });

  it('does not infer valid or best scores for cancelled versioned experiments without evaluations', () => {
    const exp = experiment([run(1, 0.99)], []);
    exp.status = 'cancelled';
    const result = selectRunEvidence(exp);
    expect(result.versioned).toBe(true);
    expect(result.best).toBeNull();
    expect(result.primaryPoints).toEqual([]);
    expect(result.allSeries).toEqual([]);
    expect(result.runs[0]).toMatchObject({ value: 0.99, kind: 'unverified', comparable: false });
    exp.iteration_state = { evaluations: [] };
    expect(selectRunEvidence(exp).primaryPoints).toEqual([]);
  });

  it('uses the frozen minimize direction and exact selector instead of a changed plan', () => {
    const first = run(1, 0.3);
    const second = run(2, 0.2);
    const frozen: ExperimentResearchContract = {
      ...contract, protocol: { primary_metric: { name: 'loss', selector: 'loss/model/dev', direction: 'minimize' } },
    };
    const evaluations = [first, second].map((source) => evaluation(source, {
      metrics: { 'loss/model/dev': [{ step: 1, value: source.primary_value }], loss: [{ step: 1, value: 0.01 }] },
    }));
    const exp = experiment([first, second], evaluations);
    exp.plan = { primary_metric: { name: 'loss', direction: 'maximize' } };
    exp.iteration_state!.research_contract = frozen;
    exp.iteration_state!.incumbent = incumbent(evaluations[1]!);
    const result = selectRunEvidence(exp);
    expect(result.direction).toBe('minimize');
    expect(result.selector).toBe('loss/model/dev');
    expect(result.best).toEqual({ step: 2, value: 0.2 });
    expect(result.runs[1]?.previousValue).toBe(0.3);
  });

  it('keeps the accepted incumbent badge when a numerically higher value fails min_delta', () => {
    const first = run(1, 0.9);
    const second = run(2, 0.95);
    const good = evaluation(first);
    const exp = experiment([first, second], [good, evaluation(second)]);
    exp.iteration_state!.research_contract = {
      ...contract, protocol: { primary_metric: { name: 'accuracy', direction: 'maximize', min_delta: 0.1 } },
    };
    exp.iteration_state!.incumbent = incumbent(good);
    const result = selectRunEvidence(exp);
    expect(result.primaryPoints).toHaveLength(2);
    expect(result.best).toEqual({ step: 1, value: 0.9 });
  });

  it('uses the bundle-selected candidate when only a result bundle is available', () => {
    const first = run(1, 0.6);
    const second = run(2, 0.99);
    const selected = evaluation(first);
    const exp = experiment([first, second], []);
    exp.iteration_state = { result_bundle: {
      contract_id: contract.contract_id, protocol_id: contract.protocol_id,
      research_contract: contract, valid_evaluations: [selected, evaluation(second)],
      selected_candidate: { candidate_id: selected.candidate_id }, selected_evaluations: [selected],
    } };
    expect(selectRunEvidence(exp).best).toEqual({ step: 1, value: 0.6 });
  });

  it('does not invent an incumbent from unselected valid evaluations', () => {
    const source = run(1, 0.99);
    const result = selectRunEvidence(experiment([source], [evaluation(source)]));
    expect(result.primaryPoints).toEqual([{ step: 1, value: 0.99 }]);
    expect(result.best).toBeNull();
  });

  it('permits explicitly legacy scores only for successful zero-exit runs', () => {
    const exp = experiment([
      run(1, 0.6), run(2, 0.99, { status: 'failed', exit_code: 1 }),
      run(3, 0.7, { exit_code: 1 }), run(4, 0.8, { status: 'running', exit_code: null }),
    ], []);
    exp.iteration_state = { no_improve_streak: 0 };
    const result = selectRunEvidence(exp);
    expect(result.versioned).toBe(false);
    expect(result.best).toEqual({ step: 1, value: 0.6 });
    expect(result.primaryPoints).toEqual([{ step: 1, value: 0.6 }]);
    expect(result.runs[0]?.kind).toBe('legacy');
    expect(result.allSeries[0]?.points.map((point) => point.value)).toEqual([0.6]);
  });

  it('rejects scalar/selector disagreement and ambiguous duplicate run references', () => {
    const source = run(1, 0.9);
    const different = evaluation(source, { metrics: { accuracy: [{ step: 1, value: 0.5 }] } });
    expect(selectRunEvidence(experiment([source], [different])).primaryPoints).toEqual([]);
    const duplicate = selectRunEvidence(experiment([source], [evaluation(source), evaluation(source)]));
    expect(duplicate.primaryPoints).toEqual([]);
    expect(duplicate.runs[0]?.reasons).toContain('ambiguous_evaluation_identity');
  });

  it('keeps identical metric steps in two valid evaluations as separate source-labeled traces', () => {
    const first = run(1, 0.6);
    const second = run(2, 0.59);
    const firstEvaluation = evaluation(first, {
      metrics: { accuracy: [{ step: 0, value: 0.55 }, { step: 1, value: 0.6 }],
        samples: [{ step: 1, value: 100 }] },
    });
    const secondEvaluation = evaluation(second, {
      metrics: { accuracy: [{ step: 0, value: 0.56 }, { step: 1, value: 0.59 }],
        samples: [{ step: 1, value: 200 }] },
    });
    const result = selectRunEvidence(experiment([first, second], [firstEvaluation, secondEvaluation]));
    const traces = selectMetricTraces(result, 'accuracy');
    expect(traces).toEqual([
      { name: 'accuracy', runSeq: 1, evaluationId: firstEvaluation.evaluation_id,
        points: [{ step: 0, value: 0.55 }, { step: 1, value: 0.6 }] },
      { name: 'accuracy', runSeq: 2, evaluationId: secondEvaluation.evaluation_id,
        points: [{ step: 0, value: 0.56 }, { step: 1, value: 0.59 }] },
    ]);
    expect(selectMetricTraces(result, 'samples').flatMap((series) => series.points.map((point) => point.value)))
      .toEqual([100, 200]);
    expect(result.primaryPoints).toEqual([{ step: 1, value: 0.6 }, { step: 2, value: 0.59 }]);
  });

  it('bounds visible histories to the selected and recent runs while retaining all traces', () => {
    const sources = Array.from({ length: 10 }, (_, index) => run(index + 1, 0.6 - index * 0.01));
    const evaluations = sources.map((source) => evaluation(source));
    const exp = experiment(sources, evaluations);
    exp.iteration_state!.incumbent = incumbent(evaluations[0]!);
    const result = selectRunEvidence(exp);
    expect(selectMetricTraces(result, 'accuracy').map((series) => series.runSeq)).toEqual([1, 6, 7, 8, 9, 10]);
    expect(selectMetricTraces(result, 'accuracy', true)).toHaveLength(10);
    expect(result.allSeries).toHaveLength(10);
  });
});

describe('RunTab evidence display', () => {
  it('shows the failed raw score as a diagnostic without an improvement color or selected badge', () => {
    setLang('zh');
    const first = run(1, 0.6);
    const failed = run(2, 0.99, { status: 'failed', exit_code: 1 });
    const good = evaluation(first);
    const exp = experiment([first, failed], [good, evaluation(failed, {
      valid: false, invalid_reasons: ['run_not_successful'],
    })]);
    exp.iteration_state!.incumbent = incumbent(good);
    const html = renderToStaticMarkup(<RunTab exp={exp} />);
    expect(html).toContain('已选 第 1 轮');
    expect(html).not.toContain('已选 第 2 轮');
    expect(html).toContain('0.9900 · 诊断值');
    expect(html).toContain('无效评估');
    expect(html).not.toContain('delta-up');
    expect(html).not.toContain('delta-down');
  });

  it('keeps invalid diagnostics and an empty accepted trend explicit in English', () => {
    setLang('en');
    const source = run(1, 0.99);
    source.reflection = { hypothesis_updates: [{ index: 0, status: 'verified' }] };
    const exp = experiment([source], [evaluation(source, {
      valid: false, invalid_reasons: ['runtime_source_changed:train.py'],
    })]);
    const html = renderToStaticMarkup(<RunTab exp={exp} />);
    expect(html).toContain('Invalid evaluation');
    expect(html).toContain('Source changed during the run');
    expect(html).toContain('0.9900 · diagnostic');
    expect(html).toContain('No valid primary metric data yet');
    expect(html).not.toContain('Selected: run');
    expect(html).not.toContain('Verified');
  });

  it('labels each evaluation trace at render time in the current language and keeps units separate', () => {
    const first = run(1, 0.6);
    const second = run(2, 0.59);
    const evaluations = [first, second].map((source) => evaluation(source, {
      metrics: { accuracy: [{ step: 1, value: source.primary_value }], samples: [{ step: 1, value: 100 }] },
    }));
    const exp = experiment([first, second], evaluations);
    setLang('zh');
    const chinese = renderToStaticMarkup(<RunTab exp={exp} />);
    expect(chinese).toContain('accuracy · 第 1 轮');
    expect(chinese).toContain('accuracy · 第 2 轮');
    expect(chinese).toContain('每条曲线对应一次运行评估');
    expect(chinese).not.toContain('samples · 第 1 轮');
    setLang('en');
    const english = renderToStaticMarkup(<RunTab exp={exp} />);
    expect(english).toContain('accuracy · Run 1');
    expect(english).toContain('accuracy · Run 2');
    expect(english).toContain('Each trace belongs to one run evaluation');
    expect(english).not.toContain('accuracy · 第 1 轮');
  });
});
