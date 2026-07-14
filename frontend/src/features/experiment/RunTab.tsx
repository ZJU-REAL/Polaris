import { useEffect, useRef, useState } from 'react';
import { Icon } from '../../components/ui/Icon';
import { StatusPill } from '../../components/ui/StatusPill';
import { MetricChart, type MetricChartSeries } from '../../components/ui/MetricChart';
import { subscribeSse } from '../../lib/sse';
import { fmtDuration, fmtTime } from '../../lib/format';
import { api, type ExperimentDetail, type ExperimentRunRead } from '../../lib/api';

/* ============================================================
   Run Tab — ExperimentRun 列表 + 实时日志（GET logs 初始 500 行
   + SSE /experiments/{id}/logs/stream 追加）+ 指标折线图。
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
          实时日志 <span className="en-label" style={{ fontSize: 11 }}>run.log</span>
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
            {paused ? '恢复滚动' : '暂停滚动'}
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
        {truncated && <div style={{ color: 'var(--text-4)' }}>…（更早日志已截断，仅显示尾部）</div>}
        {loadError && lines.length === 0 ? (
          <div style={{ color: 'var(--text-4)' }}>暂无日志（尚未开始运行，或后端不可用）</div>
        ) : lines.length === 0 ? (
          <div style={{ color: 'var(--text-4)' }}>等待日志输出…</div>
        ) : (
          lines.map((l, i) => <div key={i}>{l}</div>)
        )}
      </div>
    </div>
  );
}

function RunRow({ run }: { run: ExperimentRunRead }) {
  return (
    <tr>
      <td className="mono" style={{ fontSize: 11.5, fontWeight: 600 }}>#{run.seq}</td>
      <td className="mono" style={{ fontSize: 11.5, color: 'var(--text-2)', maxWidth: 340, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={run.command}>
        {run.command}
      </td>
      <td><StatusPill status={run.status} sm /></td>
      <td className="mono" style={{ fontSize: 11.5, color: run.exit_code === 0 ? 'var(--ok-tx)' : run.exit_code === null ? 'var(--text-4)' : 'var(--danger-tx)' }}>
        {run.exit_code ?? '—'}
      </td>
      <td className="mono muted" style={{ fontSize: 11 }}>
        {run.started_at ? `${fmtTime(run.started_at)} · ${run.finished_at ? fmtDuration(run.started_at, run.finished_at) : fmtDuration(run.started_at, null)}` : '—'}
      </td>
    </tr>
  );
}

export function RunTab({ exp, active }: { exp: ExperimentDetail; active: boolean }) {
  const runs = [...(exp.runs ?? [])].sort((a, b) => a.seq - b.seq);
  const series: MetricChartSeries[] = Object.entries(exp.metrics ?? {}).map(([name, points]) => ({
    name,
    points: (points ?? []).filter((p) => Number.isFinite(p.step) && Number.isFinite(p.value)),
  }));

  return (
    <div className="fadeup col gap20">
      {/* 指标折线图 */}
      <div className="card card-pad">
        <span className="section-h" style={{ marginBottom: 12 }}>
          <Icon name="chart" size={15} style={{ color: 'var(--accent)' }} />
          指标曲线 <span className="en-label" style={{ fontSize: 11 }}>POLARIS_METRIC</span>
        </span>
        <MetricChart series={series} />
      </div>

      {/* run 列表 */}
      <div className="card" style={{ overflow: 'hidden' }}>
        <div className="row card-pad" style={{ justifyContent: 'space-between', paddingBottom: 12 }}>
          <span className="section-h">
            <Icon name="git" size={15} style={{ color: 'var(--accent)' }} />
            运行列表 <span className="en-label" style={{ fontSize: 11 }}>Runs · {runs.length}</span>
          </span>
        </div>
        {runs.length === 0 ? (
          <div className="empty" style={{ padding: 28 }}>暂无运行记录（冒烟测试通过后开始正式运行）</div>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th style={{ width: 50 }}>run</th>
                <th>命令 command</th>
                <th style={{ width: 130 }}>状态</th>
                <th style={{ width: 70 }}>exit</th>
                <th style={{ width: 170 }}>时间 · 时长</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <RunRow key={r.id} run={r} />
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* 实时日志 */}
      <LogPanel expId={exp.id} active={active} />
    </div>
  );
}
