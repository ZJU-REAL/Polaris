import { describe, expect, it } from 'vitest';
import { setLang } from '../../../lib/i18n';
import type { ExperimentDetail, RunnerBackendSummary } from '../../../lib/api';
import { experimentSentence, experimentTone, fmtMetric, hypothesisVerdict, needsYou, summarize, summaryLine, usableBackends } from '../experimentView';

const backend = (name: string, kinds: string[], isDefault = false): RunnerBackendSummary => ({
  backend: name,
  interaction: 'batch',
  side_effects: 'filesystem',
  credential_kinds: kinds,
  licenses: [],
  is_default: isDefault,
});

describe('usable experiment backends', () => {
  it('keeps only the backends the engine can actually run (the SSH ones)', () => {
    // fmu / ngspice / openfoam are listed by the engine but always raise at run time
    const all = [backend('fmu', []), backend('ngspice', []), backend('openfoam', []), backend('python-ml', ['ssh'], true)];
    expect(usableBackends(all).map((b) => b.backend)).toEqual(['python-ml']);
  });
});

describe('experiment status wording', () => {
  it('says what it is waiting for, and marks the states that need the user', () => {
    setLang('zh');
    expect(experimentSentence('waiting_user')).toBe('等你回答一个问题，回答后继续');
    expect(experimentSentence('awaiting_gate')).toContain('等你确认');
    expect(needsYou({ status: 'waiting_user' })).toBe(true);
    expect(needsYou({ status: 'awaiting_gate' })).toBe(true);
    expect(needsYou({ status: 'running' })).toBe(false);
    expect(experimentTone('waiting_user')).toBe('warn');
    expect(experimentTone('failed')).toBe('err');
    expect(experimentTone('setup')).toBe('active');
  });
});

describe('experiment results', () => {
  const run = (seq: number, status: string, value: number | null) =>
    ({ id: String(seq), seq, command: '', status, exit_code: 0, log_path: null, metrics: null, started_at: null, finished_at: null, primary_value: value }) as unknown as ExperimentDetail['runs'][number];
  const exp = (direction: 'maximize' | 'minimize') =>
    ({
      plan: {
        primary_metric: { name: 'loss', direction },
        hypotheses: [
          { text: 'a', status: 'verified' },
          { text: 'b', status: 'supported' },
          { text: 'c', status: 'falsified' },
          { text: 'd', status: 'testing' },
        ],
      },
      runs: [run(1, 'succeeded', 0.5), run(2, 'done', 0.3), run(3, 'failed', null)],
    }) as unknown as Pick<ExperimentDetail, 'plan' | 'runs'>;

  it('picks the best round by the metric’s direction', () => {
    expect(summarize(exp('maximize')).best).toEqual({ round: 1, value: 0.5 });
    expect(summarize(exp('minimize')).best).toEqual({ round: 2, value: 0.3 });
  });

  it('counts rounds and hypothesis verdicts, including the older status names', () => {
    const s = summarize(exp('minimize'));
    expect(s.rounds).toEqual({ total: 3, succeeded: 2, failed: 1 });
    expect(s.hypotheses).toEqual({ total: 4, verified: 2, falsified: 1, open: 1 });
    expect(hypothesisVerdict('refuted')).toBe('falsified');
    expect(hypothesisVerdict(undefined)).toBe('testing');
  });

  it('writes the one-line result', () => {
    setLang('zh');
    expect(summaryLine(summarize(exp('minimize')))).toBe('loss 最好 0.3（第 2 轮） · 跑了 3 轮，1 轮失败 · 4 条假设：2 条成立，1 条不成立，1 条没有结论');
    expect(summaryLine(summarize({ plan: null, runs: [] }))).toBeNull();
  });

  it('formats metric values without trailing zeros', () => {
    expect(fmtMetric(0.22)).toBe('0.22');
    expect(fmtMetric(0.18)).toBe('0.18');
    expect(fmtMetric(17.9)).toBe('17.9');
    expect(fmtMetric(1234.5)).toBe('1235');
    expect(fmtMetric(0)).toBe('0');
  });
});
