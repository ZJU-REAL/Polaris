import { useState } from 'react';
import { Timeline, TimelineItem } from '../../components/ui/Timeline';
import { MetricChart, type MetricChartSeries } from '../../components/ui/MetricChart';
import { fmtDuration, fmtTime } from '../../lib/format';
import { type ExperimentDetail, type ExperimentRunRead, type PrimaryMetric, type RunReflection } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { StatusDot, type DotTone } from '../voyages/shared/StatusDot';
import { fmtMetric } from './experimentView';
import { stopReasonText } from './shared';

/* ============================================================
   「结果」页下半部分：各轮的主指标曲线 + 每一轮一张卡。

   一轮的卡片和任务页的步骤卡同一种读法：第几轮 + 状态 + 主指标（和上一轮比
   升了还是降了）+ AI 对这一轮的一句话；命令、时间、退出码、AI 的完整分析在「详情」里。
   ============================================================ */

/** 旧数据 / 引擎之外写入的反思可能用 summary、next 这两个键。 */
type LooseReflection = RunReflection & { summary?: string; next?: string };

function roundStatus(run: ExperimentRunRead): { tone: DotTone; label: string } {
  switch (run.status as string) {
    case 'succeeded':
    case 'done':
      return { tone: 'ok', label: tr('完成', 'Done') };
    case 'running':
      return { tone: 'active', label: tr('运行中', 'Running') };
    case 'failed':
      return { tone: 'err', label: tr('失败', 'Failed') };
    default:
      return { tone: 'idle', label: tr('未开始', 'Not started') };
  }
}

function roundMarker(run: ExperimentRunRead): { bg: string; color: string } {
  switch (run.status as string) {
    case 'succeeded':
    case 'done':
      return { bg: 'var(--ok-bg)', color: 'var(--ok-tx)' };
    case 'running':
      return { bg: 'var(--accent)', color: 'var(--on-accent)' };
    case 'failed':
      return { bg: 'var(--danger-bg)', color: 'var(--danger-tx)' };
    default:
      return { bg: 'var(--surface-2)', color: 'var(--text-3)' };
  }
}

/** 和上一轮比：「比上一轮高 0.02」。方向决定好坏，这里只说高低。 */
function deltaText(curr: number, prev: number | null): string {
  if (prev === null) return '';
  const d = curr - prev;
  if (d === 0) return tr('，和上一轮持平', ', same as the round before');
  return d > 0 ? tr(`，比上一轮高 ${fmtMetric(d)}`, `, up ${fmtMetric(d)} from the round before`) : tr(`，比上一轮低 ${fmtMetric(-d)}`, `, down ${fmtMetric(-d)} from the round before`);
}

function decisionText(decision: string | undefined): string {
  if (decision === 'improve') return tr('继续改进', 'Keep improving');
  if (decision === 'debug') return tr('修复后重跑', 'Fix and rerun');
  if (decision === 'stop') return tr('到此为止', 'Stop here');
  return '';
}

function RoundCard({ run, prevValue, metricName }: { run: ExperimentRunRead; prevValue: number | null; metricName: string }) {
  const [open, setOpen] = useState(false);
  const st = roundStatus(run);
  const hasValue = typeof run.primary_value === 'number' && Number.isFinite(run.primary_value);
  const r = (run.reflection ?? {}) as LooseReflection;
  // 一句话：AI 看到了什么；没有就不写
  const oneLine = (r.observation ?? r.summary ?? '').trim();
  const details: [string, string][] = [
    [tr('原因', 'Why'), (r.diagnosis ?? '').trim()],
    [tr('下一步', 'Next'), (r.planned_change ?? r.next ?? '').trim()],
    [tr('AI 的决定', 'AI’s decision'), decisionText(r.decision)],
    [tr('停止原因', 'Stop reason'), stopReasonText(r.stop_reason) ?? ''],
  ].filter(([, v]) => v !== '') as [string, string][];
  const when = run.started_at
    ? `${fmtTime(run.started_at)}${run.finished_at ? tr(`，用了 ${fmtDuration(run.started_at, run.finished_at)}`, `, took ${fmtDuration(run.started_at, run.finished_at)}`) : ''}`
    : '';
  return (
    <div className="card step-card" data-round={run.seq}>
      <div className="row gap8" style={{ alignItems: 'baseline', flexWrap: 'wrap' }}>
        <span className="step-title">{tr(`第 ${run.seq} 轮`, `Round ${run.seq}`)}</span>
        <span style={{ marginLeft: 'auto' }}>
          <StatusDot tone={st.tone} label={st.label} />
        </span>
      </div>
      {hasValue && (
        <div className="step-result">
          {metricName} {fmtMetric(run.primary_value as number)}
          {deltaText(run.primary_value as number, prevValue)}
        </div>
      )}
      {oneLine && <div className={'step-result' + (run.status === 'failed' ? ' is-error' : '')}>{oneLine}</div>}
      <button type="button" className="step-toggle" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        {open ? tr('收起详情', 'Hide details') : tr('详情', 'Details')}
      </button>
      {open && (
        <div className="step-details">
          {details.map(([k, v]) => (
            <div key={k} className="exp-round-detail">
              <span>{k}</span>
              <div>{v}</div>
            </div>
          ))}
          <div className="exp-round-detail">
            <span>{tr('命令', 'Command')}</span>
            <code>{run.command}</code>
          </div>
          <div className="step-meta" style={{ marginTop: 6 }}>
            {when && <span>{when}</span>}
            {run.exit_code !== null && <span>{tr(`退出码 ${run.exit_code}`, `exit code ${run.exit_code}`)}</span>}
          </div>
        </div>
      )}
    </div>
  );
}

export function RunTab({ exp }: { exp: ExperimentDetail }) {
  const runs = [...(exp.runs ?? [])].sort((a, b) => a.seq - b.seq);
  const primary = exp.plan?.primary_metric;
  const direction: PrimaryMetric['direction'] = primary?.direction === 'minimize' ? 'minimize' : 'maximize';
  const metricName = primary?.name ?? tr('主指标', 'Primary metric');

  const primaryPoints = runs
    .filter((r) => typeof r.primary_value === 'number' && Number.isFinite(r.primary_value))
    .map((r) => ({ step: r.seq, value: r.primary_value as number }));

  // 指标的形状由实验脚本决定，不一定是「一串点」：不是数组的、没有有效点的都不画
  const allSeries: MetricChartSeries[] = Object.entries(exp.metrics ?? {})
    .map(([name, points]) => ({
      name,
      points: (Array.isArray(points) ? points : []).filter((p) => p && Number.isFinite(p.step) && Number.isFinite(p.value)),
    }))
    .filter((s) => s.points.length > 0);

  let lastValue: number | null = null;
  const prevValues: (number | null)[] = runs.map((r) => {
    const prev = lastValue;
    if (typeof r.primary_value === 'number' && Number.isFinite(r.primary_value)) lastValue = r.primary_value;
    return prev;
  });

  const st = exp.iteration_state;
  const stopText = stopReasonText(st?.stopped_reason);
  const stateBits = [
    stopText ? tr(`停止原因：${stopText}`, `Stopped: ${stopText}`) : '',
    (st?.no_improve_streak ?? 0) > 0 ? tr(`已连续 ${st?.no_improve_streak} 轮没有提升`, `${st?.no_improve_streak} rounds in a row without improvement`) : '',
    (st?.debug_count ?? 0) > 0 ? tr(`修复过 ${st?.debug_count} 次`, `${st?.debug_count} fixes`) : '',
  ].filter(Boolean);

  return (
    <div>
      {primaryPoints.length > 1 && (
        <>
          <div className="task-sec-head">
            <h2 className="task-sec-title">{tr('各轮的主指标', 'Primary metric by round')}</h2>
            <span className="step-note">
              {metricName} · {direction === 'minimize' ? tr('越低越好', 'lower is better') : tr('越高越好', 'higher is better')}
            </span>
          </div>
          <MetricChart series={[{ name: metricName, points: primaryPoints }]} height={180} />
        </>
      )}

      <div className="task-sec-head">
        <h2 className="task-sec-title">{tr('各轮记录', 'Rounds')}</h2>
        {stateBits.length > 0 && <span className="step-note">{stateBits.join(' · ')}</span>}
      </div>
      <Timeline>
        {runs.map((r, i) => {
          const m = roundMarker(r);
          return (
            <TimelineItem key={r.id} marker={r.seq} markerBg={m.bg} markerColor={m.color} last={i === runs.length - 1}>
              <RoundCard run={r} prevValue={prevValues[i] ?? null} metricName={metricName} />
            </TimelineItem>
          );
        })}
      </Timeline>

      {allSeries.length > 0 && (
        <>
          <div className="task-sec-head">
            <h2 className="task-sec-title">{tr('全部指标', 'All metrics')}</h2>
          </div>
          <MetricChart series={allSeries} />
        </>
      )}
    </div>
  );
}
