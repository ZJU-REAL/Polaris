import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Icon } from '../../components/ui/Icon';
import { StatusPill } from '../../components/ui/StatusPill';
import { Timeline, TimelineItem } from '../../components/ui/Timeline';
import { MetricChart, type MetricChartSeries } from '../../components/ui/MetricChart';
import { subscribeSse } from '../../lib/sse';
import { fmtDuration, fmtTime } from '../../lib/format';
import {
  api,
  type ExperimentDetail,
  type ExperimentRunRead,
  type IterationDecision,
  type PrimaryMetric,
  type RunReflection,
} from '../../lib/api';
import { tr } from '../../lib/i18n';
import { stopReasonText } from './shared';

/* ============================================================
   Run Tab —「运行与迭代」：
   - 顶部主指标趋势（primary_value by seq，方向感知）+ 迭代状态条
   - 迭代时间线：每轮一卡（#seq、状态、主指标 + 与上轮差值、
     AI 决定徽章 improve/debug/stop、reflection 三字段折叠）
   - 全部指标曲线（POLARIS_METRIC by step）
   - 实时日志（GET logs 初始 500 行 + SSE 追加，跟踪最新一轮）
   ============================================================ */

const MAX_LOG_LINES = 2000;

/** SSE data → 日志行数组：兼容 JSON {line}/{lines} 与纯文本。 */
function parseLogEvent(data: string): string[] {
  try {
    const p: unknown = JSON.parse(data);
    if (p && typeof p === 'object') {
      const rec = p as { line?: unknown; lines?: unknown; message?: unknown };
      if (typeof rec.line === 'string') return [rec.line];
      if (Array.isArray(rec.lines)) return rec.lines.filter((l): l is string => typeof l === 'string');
      if (typeof rec.message === 'string') return [rec.message];
      return [];
    }
    if (typeof p === 'string') return [p];
  } catch {
    /* 非 JSON：按纯文本处理 */
  }
  return data.split('\n').filter((l) => l !== '');
}

function LogPanel({ expId, active }: { expId: string; active: boolean }) {
  const [lines, setLines] = useState<string[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [paused, setPaused] = useState(false);
  const [live, setLive] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);

  // 初始拉取尾部 500 行
  useEffect(() => {
    let cancelled = false;
    setLines([]);
    setLoadError(false);
    api
      .getExperimentLogs(expId, { tail: 500 })
      .then((r) => {
        if (cancelled) return;
        setLines(r.lines.slice(-MAX_LOG_LINES));
        setTruncated(r.truncated);
      })
      .catch(() => {
        if (!cancelled) setLoadError(true);
      });
    return () => {
      cancelled = true;
    };
  }, [expId]);

  // 活动状态：SSE 追加
  useEffect(() => {
    if (!active) return;
    const stop = subscribeSse(`/experiments/${expId}/logs/stream`, {
      onOpen: () => setLive(true),
      onError: () => setLive(false),
      onEvent: (_event, data) => {
        const next = parseLogEvent(data);
        if (next.length > 0) setLines((l) => [...l, ...next].slice(-MAX_LOG_LINES));
      },
    });
    return () => {
      stop();
      setLive(false);
    };
  }, [expId, active]);

  // 自动滚底（未暂停时）
  useEffect(() => {
    if (paused) return;
    const box = boxRef.current;
    if (box) box.scrollTop = box.scrollHeight;
  }, [lines, paused]);

  return (
    <div className="card" style={{ overflow: 'hidden' }}>
      <div className="row card-pad" style={{ justifyContent: 'space-between', paddingBottom: 12 }}>
        <span className="section-h">
          <Icon name="file" size={15} style={{ color: 'var(--accent)' }} />
          {tr('实时日志', 'Live log')} <span className="en-label" style={{ fontSize: 11 }}>run.log · {tr('跟踪最新一轮', 'follows the latest run')}</span>
        </span>
        <div className="row gap8">
          {live && (
            <span className="pill sm" style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>
              <span className="dot pulse" />
              LIVE
            </span>
          )}
          <button className="btn btn-soft sm" onClick={() => setPaused((p) => !p)}>
            <Icon name={paused ? 'play' : 'pause'} size={12} />
            {paused ? tr('恢复滚动', 'Resume scroll') : tr('暂停滚动', 'Pause scroll')}
          </button>
        </div>
      </div>
      <div
        ref={boxRef}
        className="scroll"
        style={{
          fontFamily: 'var(--mono)',
          fontSize: 11,
          lineHeight: 1.6,
          background: 'var(--surface-2)',
          borderTop: '0.5px solid var(--border)',
          padding: '10px 16px',
          height: 320,
          overflowY: 'auto',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-all',
        }}
      >
        {truncated && <div style={{ color: 'var(--text-4)' }}>{tr('…（更早日志已截断，仅显示尾部）', '… (earlier log truncated, showing the tail)')}</div>}
        {loadError && lines.length === 0 ? (
          <div style={{ color: 'var(--text-4)' }}>{tr('暂无日志（尚未开始运行，或后端不可用）', 'No logs yet (not running, or backend unavailable)')}</div>
        ) : lines.length === 0 ? (
          <div style={{ color: 'var(--text-4)' }}>{tr('等待日志输出…', 'Waiting for log output…')}</div>
        ) : (
          lines.map((l, i) => <div key={i}>{l}</div>)
        )}
      </div>
    </div>
  );
}

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

function runMarker(run: ExperimentRunRead): { bg: string; color: string } {
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

/** 迭代时间线的一轮卡片。 */
function IterationCard({
  run,
  prevValue,
  direction,
}: {
  run: ExperimentRunRead;
  prevValue: number | null;
  direction: PrimaryMetric['direction'];
}) {
  const hasValue = typeof run.primary_value === 'number' && Number.isFinite(run.primary_value);
  return (
    <div className="card" style={{ padding: '12px 16px' }}>
      <div className="row gap8" style={{ flexWrap: 'wrap' }}>
        <span className="mono" style={{ fontSize: 12, fontWeight: 700 }}>{tr(`第 ${run.seq} 轮`, `Run ${run.seq}`)}</span>
        <StatusPill status={run.status} sm />
        {hasValue ? (
          <PrimaryValue curr={run.primary_value as number} prev={prevValue} direction={direction} />
        ) : (
          <span className="mono muted" style={{ fontSize: 11 }}>{tr('主指标 —', 'metric —')}</span>
        )}
        <div style={{ marginLeft: 'auto' }}>
          {run.reflection?.decision && <DecisionBadge decision={run.reflection.decision} />}
        </div>
      </div>
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
      value: `${st?.debug_count ?? 0} / 3`,
      warn: (st?.debug_count ?? 0) >= 2,
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

export function RunTab({ exp, active }: { exp: ExperimentDetail; active: boolean }) {
  const runs = [...(exp.runs ?? [])].sort((a, b) => a.seq - b.seq);
  const primary = exp.plan?.primary_metric;
  const direction: PrimaryMetric['direction'] = primary?.direction === 'minimize' ? 'minimize' : 'maximize';

  // 主指标趋势：primary_value by seq
  const primaryPoints = runs
    .filter((r) => typeof r.primary_value === 'number' && Number.isFinite(r.primary_value))
    .map((r) => ({ step: r.seq, value: r.primary_value as number }));
  const best =
    primaryPoints.length > 0
      ? primaryPoints.reduce((a, b) =>
          direction === 'minimize' ? (b.value < a.value ? b : a) : (b.value > a.value ? b : a),
        )
      : null;

  // 全部指标（POLARIS_METRIC by step）
  const allSeries: MetricChartSeries[] = Object.entries(exp.metrics ?? {}).map(([name, points]) => ({
    name,
    points: (points ?? []).filter((p) => Number.isFinite(p.step) && Number.isFinite(p.value)),
  }));

  // 供 Delta 对比的上一轮有值 run
  let lastValue: number | null = null;
  const prevValues: (number | null)[] = runs.map((r) => {
    const prev = lastValue;
    if (typeof r.primary_value === 'number' && Number.isFinite(r.primary_value)) {
      lastValue = r.primary_value;
    }
    return prev;
  });

  return (
    <div className="fadeup col gap20">
      {/* 主指标趋势 + 迭代状态 */}
      <div className="card card-pad">
        <div className="row gap8" style={{ marginBottom: 12, flexWrap: 'wrap' }}>
          <span className="section-h">
            <Icon name="chart" size={15} style={{ color: 'var(--accent)' }} />
            {tr('主指标趋势', 'Primary metric trend')} <span className="en-label" style={{ fontSize: 11 }}>{primary?.name ?? tr('主指标', 'primary metric')}</span>
          </span>
          {primary && (
            <span className="pill sm" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
              {direction === 'minimize' ? tr('↓ 越低越好', '↓ lower is better') : tr('↑ 越高越好', '↑ higher is better')}
            </span>
          )}
          {best && (
            <span className="pill sm mono" style={{ marginLeft: 'auto', background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>
              {tr(`最佳 第 ${best.step} 轮`, `Best: run ${best.step}`)} · {fmtMetric(best.value)}
            </span>
          )}
        </div>
        {primaryPoints.length > 0 ? (
          <MetricChart series={[{ name: primary?.name ?? 'primary', points: primaryPoints }]} height={180} />
        ) : (
          <div className="empty" style={{ padding: 26, fontSize: 12.5 }}>
            {tr('暂无主指标数据 · 每轮运行结束后系统会解析出主指标值画在这里', 'No primary metric data yet — each run’s value is parsed and plotted here once it finishes')}
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
              '还没有运行记录 · 冒烟测试通过后开始自动迭代：每轮跑完 AI 会分析结果，决定继续改进、修错重试或停止',
              'No runs yet — auto-iteration starts after the smoke test passes: the AI analyzes each run and decides to improve, debug or stop',
            )}
          </div>
        ) : (
          <Timeline>
            {runs.map((r, i) => {
              const m = runMarker(r);
              return (
                <TimelineItem key={r.id} marker={`#${r.seq}`} markerBg={m.bg} markerColor={m.color} last={i === runs.length - 1}>
                  <IterationCard run={r} prevValue={prevValues[i] ?? null} direction={direction} />
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
            {tr('全部指标曲线', 'All metric curves')} <span className="en-label" style={{ fontSize: 11 }}>POLARIS_METRIC</span>
          </span>
          <MetricChart series={allSeries} />
        </div>
      )}

      {/* 实时日志 */}
      <LogPanel expId={exp.id} active={active} />
    </div>
  );
}
