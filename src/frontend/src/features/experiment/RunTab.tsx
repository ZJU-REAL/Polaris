import { useState } from 'react';
import type { ReactNode } from 'react';
import { Icon } from '../../components/ui/Icon';
import { StatusPill } from '../../components/ui/StatusPill';
import { Timeline, TimelineItem } from '../../components/ui/Timeline';
import { MetricChart } from '../../components/ui/MetricChart';
import { fmtDuration, fmtTime } from '../../lib/format';
import {
  type ExperimentDetail,
  type ExperimentRunRead,
  type IterationDecision,
  type PrimaryMetric,
  type RunReflection,
} from '../../lib/api';
import { tr } from '../../lib/i18n';
import { stopReasonText } from './shared';
import { selectMetricTraces, selectRunEvidence, type RunEvidence } from './runEvidence';

/* ============================================================
   Run Tab —运行与迭代：
   - 顶部主指标趋势（primary_value by seq，方向感知）+ 迭代状态条
   - 迭代时间线：每轮一卡（#seq、状态、主指标 + 与上轮差值、
     AI 决定徽章 improve/debug/stop、reflection 三字段折叠）
   - 全部指标曲线（POLARIS_METRIC by step）
   实时日志已并入运行台（ConsoleTab 的「脚本输出」源）。
   ============================================================ */

/* ---------------- 迭代小件 ---------------- */

function fmtMetric(v: number): string {
  const a = Math.abs(v);
  if (a >= 1000) return v.toFixed(0);
  if (a >= 100) return v.toFixed(1);
  if (a >= 1) return v.toFixed(3);
  return v.toFixed(4);
}

/** AI 决定徽章：improve 蓝 / debug 橙 / stop 灰。 */
function DecisionBadge({ decision }: { decision: IterationDecision | string }) {
  const map: Record<string, [string, string, string]> = {
    improve: ['var(--accent-soft)', 'var(--accent-text)', tr('↻ 继续改进', '↻ Keep improving')],
    debug: ['var(--warn-bg)', 'var(--warn-tx)', tr('⚒ 修错重试', '⚒ Debug & retry')],
    stop: ['var(--surface-3)', 'var(--text-3)', tr('■ 停止迭代', '■ Stop iterating')],
  };
  const meta = map[decision];
  if (!meta) return null;
  const [bg, c, t] = meta;
  return (
    <span className="pill sm" style={{ background: bg, color: c, fontWeight: 650, flexShrink: 0 }}>
      {t}
    </span>
  );
}

/** 主指标值 + 与上轮差值（方向感知：改善绿 / 变差红 / 持平灰）。 */
function PrimaryValue({
  curr,
  prev,
  direction,
}: {
  curr: number;
  prev: number | null;
  direction: PrimaryMetric['direction'];
}) {
  const delta = prev === null ? null : curr - prev;
  let deltaEl: ReactNode = null;
  if (delta !== null) {
    if (delta === 0) {
      deltaEl = (
        <span className="mono" style={{ fontSize: 11, color: 'var(--text-4)', fontWeight: 650 }}>{tr('— 持平', '— flat')}</span>
      );
    } else {
      const improved = direction === 'minimize' ? delta < 0 : delta > 0;
      deltaEl = (
        <span className={improved ? 'delta-up' : 'delta-down'} style={{ fontSize: 11 }}>
          {delta > 0 ? '▲' : '▼'} {fmtMetric(Math.abs(delta))}
        </span>
      );
    }
  }
  return (
    <span className="row gap6" style={{ flexShrink: 0 }}>
      <span className="mono" style={{ fontSize: 13, fontWeight: 700 }}>{fmtMetric(curr)}</span>
      {deltaEl}
    </span>
  );
}

/** reflection 三字段折叠展示（observation / diagnosis / planned_change）。 */
function ReflectionBlock({ reflection }: { reflection: RunReflection }) {
  const [open, setOpen] = useState(false);
  const fields: [string, string, string | undefined][] = [
    [tr('看到了什么', 'What happened'), 'observation', reflection.observation],
    [tr('原因分析', 'Diagnosis'), 'diagnosis', reflection.diagnosis],
    [tr('下一步改动', 'Next change'), 'planned_change', reflection.planned_change],
  ];
  const present = fields.filter(([, , v]) => !!v && v.trim() !== '');
  const stopText = stopReasonText(reflection.stop_reason);
  if (present.length === 0 && !stopText) return null;

  return (
    <div style={{ marginTop: 9 }}>
      <button
        className="row gap6"
        onClick={() => setOpen((o) => !o)}
        style={{
          border: 'none',
          background: 'transparent',
          cursor: 'pointer',
          padding: 0,
          fontSize: 11.5,
          fontWeight: 650,
          color: 'var(--accent-text)',
          fontFamily: 'var(--sans)',
        }}
      >
        <Icon name="sparkle" size={12} />
        {tr('AI 分析', 'AI reflection')}
        <Icon
          name="chevDown"
          size={11}
          style={{ transform: open ? 'rotate(180deg)' : 'none', transition: 'transform .15s' }}
        />
      </button>
      {open && (
        <div
          className="col gap8"
          style={{
            marginTop: 8,
            padding: '10px 12px',
            borderRadius: 8,
            background: 'var(--surface-2)',
            border: '0.5px solid var(--border)',
          }}
        >
          {present.map(([zh, en, v]) => (
            <div key={en}>
              <div style={{ fontSize: 10.5, color: 'var(--text-3)', fontWeight: 650, marginBottom: 3 }}>
                {zh}
              </div>
              <div style={{ fontSize: 12, color: 'var(--text-2)', lineHeight: 1.6, whiteSpace: 'pre-wrap' }}>{v}</div>
            </div>
          ))}
          {stopText && (
            <div>
              <div style={{ fontSize: 10.5, color: 'var(--text-3)', fontWeight: 650, marginBottom: 3 }}>
                {tr('停止原因', 'Stop reason')}
              </div>
              <div style={{ fontSize: 12, color: 'var(--text-2)', lineHeight: 1.6 }}>{stopText}</div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function runMarker(run: ExperimentRunRead, evidence: RunEvidence): { bg: string; color: string } {
  if (run.status === 'succeeded' && evidence.kind === 'invalid') {
    return { bg: 'var(--warn-bg)', color: 'var(--warn-tx)' };
  }
  switch (run.status) {
    case 'succeeded':
      return { bg: 'var(--ok-bg)', color: 'var(--ok-tx)' };
    case 'running':
      return { bg: 'var(--accent)', color: '#fff' };
    case 'failed':
      return { bg: 'var(--danger-bg)', color: 'var(--danger-tx)' };
    default:
      return { bg: 'var(--surface-2)', color: 'var(--text-3)' };
  }
}

function invalidReasonText(reason: string): string {
  if (reason.startsWith('runtime_source_changed:')) {
    return tr('运行时源码已变化', 'Source changed during the run');
  }
  if (reason.startsWith('protected_file') || reason.startsWith('candidate_source')) {
    return tr('源码保护检查未通过', 'Source protection check failed');
  }
  if (reason.includes('sample_count')) return tr('样本数量缺失或不完整', 'Missing or incomplete sample count');
  if (reason === 'missing_or_unpaired_conditions') return tr('缺少同轮配对对照', 'Missing paired controls in this run');
  if (reason === 'run_not_successful') return tr('运行未成功', 'The run did not succeed');
  if (reason.includes('protocol') || reason.includes('contract')) return tr('评估协议不一致', 'Evaluation protocol mismatch');
  if (reason.includes('identity')) return tr('评估来源身份不一致', 'Evaluation source identity mismatch');
  if (reason.includes('metric')) return tr('主指标缺失或无效', 'Missing or invalid metric');
  return tr('未通过结果验收', 'Result validation failed');
}

/** 迭代时间线的一轮卡片。 */
function IterationCard({
  run,
  evidence,
  direction,
}: {
  run: ExperimentRunRead;
  evidence: RunEvidence;
  direction: PrimaryMetric['direction'];
}) {
  const label = evidence.kind === 'valid' ? tr('有效评估', 'Valid evaluation')
    : evidence.kind === 'legacy' ? tr('历史成功运行', 'Legacy successful run')
      : evidence.kind === 'invalid' ? tr('无效评估', 'Invalid evaluation')
        : run.status === 'running' ? tr('待验收', 'Awaiting evaluation') : tr('未验收', 'Unverified');
  return (
    <div className="card" style={{ padding: '12px 16px' }}>
      <div className="row gap8" style={{ flexWrap: 'wrap' }}>
        <span className="mono" style={{ fontSize: 12, fontWeight: 700 }}>{tr(`第 ${run.seq} 轮`, `Run ${run.seq}`)}</span>
        <StatusPill status={run.status} sm />
        {evidence.value !== null ? evidence.comparable ? (
          <PrimaryValue curr={evidence.value} prev={evidence.previousValue} direction={direction} />
        ) : (
          <span className="mono muted" style={{ fontSize: 13, fontWeight: 700 }}>
            {fmtMetric(evidence.value)} · {tr('诊断值', 'diagnostic')}
          </span>
        ) : (
          <span className="mono muted" style={{ fontSize: 11 }}>{tr('主指标 —', 'metric —')}</span>
        )}
        <span className="pill sm" style={{
          background: evidence.kind === 'invalid' ? 'var(--warn-bg)' : 'var(--surface-2)',
          color: evidence.kind === 'invalid' ? 'var(--warn-tx)' : 'var(--text-3)',
        }}>{label}</span>
        <div style={{ marginLeft: 'auto' }}>
          {run.reflection?.decision && <DecisionBadge decision={run.reflection.decision} />}
        </div>
      </div>
      {evidence.kind === 'invalid' && evidence.reasons.length > 0 && (
        <div style={{ marginTop: 6, fontSize: 11, color: 'var(--text-3)' }}>
          {[...new Set(evidence.reasons.map(invalidReasonText))].join(tr('；', '; '))}
        </div>
      )}
      <div
        className="mono"
        title={run.command}
        style={{
          marginTop: 7,
          fontSize: 11,
          color: 'var(--text-3)',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
      >
        $ {run.command}
      </div>
      <div className="mono muted" style={{ fontSize: 10.5, marginTop: 4 }}>
        {run.started_at
          ? `${fmtTime(run.started_at)} · ${run.finished_at ? `${tr('耗时', 'took')} ${fmtDuration(run.started_at, run.finished_at)}` : tr('运行中', 'running')}`
          : tr('未开始', 'not started')}
        {run.exit_code !== null && (
          <span style={{ color: run.exit_code === 0 ? 'var(--ok-tx)' : 'var(--danger-tx)', marginLeft: 8 }}>
            exit {run.exit_code}
          </span>
        )}
      </div>
      {run.reflection && <ReflectionBlock reflection={run.reflection} />}
    </div>
  );
}

/** 迭代状态条：轮数 / 无提升计数 / 修错计数 / 停止原因。 */
function IterationStateBar({ exp, runCount }: { exp: ExperimentDetail; runCount: number }) {
  const st = exp.iteration_state;
  const stopText = stopReasonText(st?.stopped_reason);
  const noImproveLimit = exp.budget?.no_improve_stop ?? 2;
  const items: { label: string; value: string; warn?: boolean }[] = [
    {
      label: tr('已跑轮数', 'Runs done'),
      value: exp.budget?.max_runs ? `${runCount} / ${exp.budget.max_runs}` : String(runCount),
    },
    {
      label: tr('连续无提升', 'No-gain streak'),
      value: tr(`${st?.no_improve_streak ?? 0} / ${noImproveLimit} 轮`, `${st?.no_improve_streak ?? 0} / ${noImproveLimit} runs`),
      warn: (st?.no_improve_streak ?? 0) >= noImproveLimit - 1 && (st?.no_improve_streak ?? 0) > 0,
    },
    {
      label: tr('修错次数', 'Debug attempts'),
      value: String(st?.debug_count ?? 0),
    },
  ];
  return (
    <div className="row gap12" style={{ flexWrap: 'wrap', marginTop: 12 }}>
      {items.map((it) => (
        <span
          key={it.label}
          className="pill sm"
          style={{
            background: it.warn ? 'var(--warn-bg)' : 'var(--surface-2)',
            color: it.warn ? 'var(--warn-tx)' : 'var(--text-2)',
          }}
        >
          {it.label} <span className="mono" style={{ fontWeight: 700 }}>{it.value}</span>
        </span>
      ))}
      {stopText && (
        <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-2)' }}>
          <Icon name="pause" size={11} />
          {tr('已停止：', 'Stopped: ')}{stopText}
        </span>
      )}
    </div>
  );
}

/* ---------------- Tab 主体 ---------------- */

export function RunTab({ exp }: { exp: ExperimentDetail }) {
  const [metricChoice, setMetricChoice] = useState<string | null>(null);
  const [showAllTraces, setShowAllTraces] = useState(false);
  const evidence = selectRunEvidence(exp);
  const { direction, primaryPoints, best, allSeries } = evidence;
  const runs = evidence.runs;
  const metricLabel = evidence.selector || tr('主指标', 'primary metric');
  const metricNames = [...new Set(allSeries.map((series) => series.name))];
  const activeMetric = metricChoice && metricNames.includes(metricChoice) ? metricChoice
    : metricNames.includes(evidence.selector) ? evidence.selector : metricNames[0] ?? '';
  const traceCount = allSeries.filter((series) => series.name === activeMetric).length;
  const traces = selectMetricTraces(evidence, activeMetric, showAllTraces);
  const metricSeries = traces.map((series) => ({
    name: tr(`${series.name} · 第 ${series.runSeq} 轮`, `${series.name} · Run ${series.runSeq}`),
    points: series.points,
  }));

  return (
    <div className="fadeup col gap20">
      {/* 主指标趋势 + 迭代状态 */}
      <div className="card card-pad">
        <div className="row gap8" style={{ marginBottom: 12, flexWrap: 'wrap' }}>
          <span className="section-h">
            <Icon name="chart" size={15} style={{ color: 'var(--accent)' }} />
            {tr('主指标趋势', 'Primary metric trend')} <span className="en-label" style={{ fontSize: 11 }}>{metricLabel}</span>
          </span>
          {evidence.selector && (
            <span className="pill sm" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
              {direction === 'minimize' ? tr('↓ 越低越好', '↓ lower is better') : tr('↑ 越高越好', '↑ higher is better')}
            </span>
          )}
          {best && (
            <span className="pill sm mono" style={{ marginLeft: 'auto', background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>
              {evidence.versioned ? tr(`已选 第 ${best.step} 轮`, `Selected: run ${best.step}`)
                : tr(`历史最佳 第 ${best.step} 轮`, `Legacy best: run ${best.step}`)} · {fmtMetric(best.value)}
            </span>
          )}
        </div>
        {primaryPoints.length > 0 ? (
          <MetricChart series={[{ name: metricLabel, points: primaryPoints }]} height={180} />
        ) : (
          <div className="empty" style={{ padding: 26, fontSize: 12.5 }}>
            {evidence.versioned ? tr('暂无有效主指标数据', 'No valid primary metric data yet')
              : tr('暂无成功运行的主指标数据', 'No primary metrics from successful legacy runs yet')}
          </div>
        )}
        <IterationStateBar exp={exp} runCount={runs.length} />
      </div>

      {/* 迭代时间线 */}
      <div className="card card-pad">
        <span className="section-h" style={{ marginBottom: 14 }}>
          <Icon name="refresh" size={15} style={{ color: 'var(--accent)' }} />
          {tr('自动迭代过程', 'Auto-iteration')} <span className="en-label" style={{ fontSize: 11 }}>{runs.length}</span>
        </span>
        {runs.length === 0 ? (
          <div className="empty" style={{ padding: 28 }}>
            {tr(
              '还没有运行记录 · 冒烟测试通过后开始自动迭代',
              'No runs yet — auto-iteration starts after the smoke test passes',
            )}
          </div>
        ) : (
          <Timeline>
            {runs.map((entry, i) => {
              const r = entry.run;
              const m = runMarker(r, entry);
              return (
                <TimelineItem key={r.id} marker={`#${r.seq}`} markerBg={m.bg} markerColor={m.color} last={i === runs.length - 1}>
                  <IterationCard run={r} evidence={entry} direction={direction} />
                </TimelineItem>
              );
            })}
          </Timeline>
        )}
      </div>

      {/* 全部指标曲线 */}
      {allSeries.length > 0 && (
        <div className="card card-pad">
          <span className="section-h" style={{ marginBottom: 12 }}>
            <Icon name="chart" size={15} style={{ color: 'var(--accent)' }} />
            {evidence.versioned ? tr('有效评估指标曲线', 'Valid evaluation metrics')
              : tr('历史成功运行指标', 'Successful legacy run metrics')} <span className="en-label" style={{ fontSize: 11 }}>POLARIS_METRIC</span>
          </span>
          <div className="row gap8" style={{ marginBottom: 12, flexWrap: 'wrap' }}>
            <label className="row gap6" style={{ fontSize: 11, color: 'var(--text-2)' }}>
              {tr('指标', 'Metric')}
              <select
                className="input sm"
                value={activeMetric}
                onChange={(event) => setMetricChoice(event.target.value)}
                style={{ width: 'auto', maxWidth: 320, fontSize: 11 }}
              >
                {metricNames.map((name) => <option key={name} value={name}>{name}</option>)}
              </select>
            </label>
            <span style={{ fontSize: 11, color: 'var(--text-3)' }}>
              {tr('每条曲线对应一次运行评估', 'Each trace belongs to one run evaluation')}
            </span>
            {traceCount > 6 && (
              <button className="btn btn-ghost sm" onClick={() => setShowAllTraces((value) => !value)}>
                {showAllTraces ? tr('显示已选与最近轮次', 'Show selected and recent runs')
                  : tr(`显示全部 ${traceCount} 轮`, `Show all ${traceCount} runs`)}
              </button>
            )}
            {traces.length < traceCount && (
              <span style={{ fontSize: 11, color: 'var(--text-3)' }}>
                {tr(`已选与最近轮次 · ${traces.length} / ${traceCount}`, `Selected and recent · ${traces.length} / ${traceCount}`)}
              </span>
            )}
          </div>
          <MetricChart series={metricSeries} />
        </div>
      )}
    </div>
  );
}
