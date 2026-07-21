import { useMemo, useRef, useState } from 'react';
import { tr } from '../../lib/i18n';

/* ============================================================
   MetricChart — 轻量 SVG 自绘多序列折线图（零依赖）。
   数据源：ExperimentDetail.metrics {name: [{step, value}]}。
   坐标轴 + 网格线 + 图例 + hover 提示（最近 step 竖线 + tooltip）。
   颜色全部使用 tokens 语义色，组件内不写死颜色。
   ============================================================ */

export interface MetricChartPoint {
  step: number;
  value: number;
}

export interface MetricChartSeries {
  name: string;
  points: MetricChartPoint[];
}

export interface MetricChartProps {
  series: MetricChartSeries[];
  /** viewBox 高度（宽度固定 560，随容器缩放）。 */
  height?: number;
  /** 可选 baseline 虚线（如论文基线值）。 */
  baseline?: number;
}

const COLORS = ['var(--accent)', 'var(--ok)', 'var(--warn)', 'var(--danger)', 'var(--accent-text)'];

function fmtVal(v: number): string {
  const a = Math.abs(v);
  if (a >= 1000) return v.toFixed(0);
  if (a >= 100) return v.toFixed(1);
  if (a >= 1) return v.toFixed(2);
  return v.toFixed(3);
}

interface Hover {
  step: number;
  /** viewBox 坐标系的 x */
  x: number;
  /** 容器内百分比位置（tooltip 定位用） */
  pctX: number;
  entries: { name: string; color: string; value: number }[];
}

export function MetricChart({ series, height = 220, baseline }: MetricChartProps) {
  const W = 560;
  const H = height;
  const pad = { l: 48, r: 14, t: 14, b: 26 };
  const svgRef = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<Hover | null>(null);

  const data = useMemo(
    () =>
      series
        .filter((s) => s.points.length > 0)
        .map((s, i) => ({
          name: s.name,
          color: COLORS[i % COLORS.length] as string,
          points: [...s.points].sort((a, b) => a.step - b.step),
        })),
    [series],
  );

  const { lo, hi, minStep, maxStep, allSteps } = useMemo(() => {
    const vals: number[] = [];
    const stepSet = new Set<number>();
    for (const s of data) {
      for (const p of s.points) {
        vals.push(p.value);
        stepSet.add(p.step);
      }
    }
    if (baseline !== undefined) vals.push(baseline);
    const steps = [...stepSet].sort((a, b) => a - b);
    let vLo = Math.min(...vals);
    let vHi = Math.max(...vals);
    if (!Number.isFinite(vLo) || !Number.isFinite(vHi)) {
      vLo = 0;
      vHi = 1;
    }
    if (vHi === vLo) {
      vHi += Math.abs(vHi) * 0.1 || 1;
      vLo -= Math.abs(vLo) * 0.1 || 1;
    } else {
      const m = (vHi - vLo) * 0.08;
      vHi += m;
      vLo -= m;
    }
    return {
      lo: vLo,
      hi: vHi,
      minStep: steps.length > 0 ? (steps[0] as number) : 0,
      maxStep: steps.length > 0 ? (steps[steps.length - 1] as number) : 1,
      allSteps: steps,
    };
  }, [data, baseline]);

  if (data.length === 0) {
    return (
      <div className="empty" style={{ padding: 30, fontSize: 12.5 }}>
        {tr('暂无指标数据 · 运行日志中的 POLARIS_METRIC 行会被解析到这里', 'No metric data yet · POLARIS_METRIC lines in run logs are parsed into this chart')}
      </div>
    );
  }

  const x = (st: number) =>
    pad.l + (maxStep === minStep ? 0.5 : (st - minStep) / (maxStep - minStep)) * (W - pad.l - pad.r);
  const y = (v: number) => pad.t + (1 - (v - lo) / (hi - lo)) * (H - pad.t - pad.b);

  function onMove(e: React.MouseEvent<SVGSVGElement>) {
    const svg = svgRef.current;
    if (!svg || allSteps.length === 0) return;
    const rect = svg.getBoundingClientRect();
    const vx = ((e.clientX - rect.left) / rect.width) * W;
    let nearest = allSteps[0] as number;
    let best = Infinity;
    for (const st of allSteps) {
      const d = Math.abs(x(st) - vx);
      if (d < best) {
        best = d;
        nearest = st;
      }
    }
    const entries: Hover['entries'] = [];
    for (const s of data) {
      const p = s.points.find((pt) => pt.step === nearest);
      if (p) entries.push({ name: s.name, color: s.color, value: p.value });
    }
    if (entries.length === 0) {
      setHover(null);
      return;
    }
    setHover({ step: nearest, x: x(nearest), pctX: (x(nearest) / W) * 100, entries });
  }

  const gridTs = [0, 0.25, 0.5, 0.75, 1];
  const xLabels =
    maxStep === minStep ? [minStep] : [minStep, Math.round((minStep + maxStep) / 2), maxStep];

  return (
    <div style={{ position: 'relative' }}>
      <svg
        ref={svgRef}
        width="100%"
        viewBox={`0 0 ${W} ${H}`}
        style={{ display: 'block' }}
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
      >
        {/* 网格线 + y 轴刻度 */}
        {gridTs.map((t, i) => {
          const val = lo + t * (hi - lo);
          return (
            <g key={i}>
              <line x1={pad.l} y1={y(val)} x2={W - pad.r} y2={y(val)} stroke="var(--border)" strokeWidth="0.5" />
              <text x={pad.l - 8} y={y(val) + 3} textAnchor="end" fontSize="9" fill="var(--text-4)" fontFamily="var(--mono)">
                {fmtVal(val)}
              </text>
            </g>
          );
        })}
        {/* x 轴刻度 */}
        {xLabels.map((st, i) => (
          <text key={i} x={x(st)} y={H - 8} textAnchor="middle" fontSize="9" fill="var(--text-4)" fontFamily="var(--mono)">
            {st}
          </text>
        ))}
        {/* baseline 虚线 */}
        {baseline !== undefined && (
          <>
            <line x1={pad.l} y1={y(baseline)} x2={W - pad.r} y2={y(baseline)} stroke="var(--text-3)" strokeWidth="1" strokeDasharray="4 3" />
            <text x={W - pad.r} y={y(baseline) - 5} textAnchor="end" fontSize="9" fill="var(--text-3)" fontFamily="var(--mono)">
              baseline {fmtVal(baseline)}
            </text>
          </>
        )}
        {/* hover 竖线 */}
        {hover && (
          <line x1={hover.x} y1={pad.t} x2={hover.x} y2={H - pad.b} stroke="var(--border-2)" strokeWidth="1" strokeDasharray="3 3" />
        )}
        {/* 序列折线 + 数据点 */}
        {data.map((s) => (
          <g key={s.name}>
            {s.points.length > 1 && (
              <polyline
                points={s.points.map((p) => `${x(p.step)},${y(p.value)}`).join(' ')}
                fill="none"
                stroke={s.color}
                strokeWidth="2"
                strokeLinejoin="round"
              />
            )}
            {s.points.map((p, i) => (
              <circle
                key={i}
                cx={x(p.step)}
                cy={y(p.value)}
                r={hover?.step === p.step ? 4 : 2.5}
                fill={s.color}
                stroke="var(--surface)"
                strokeWidth="1"
              />
            ))}
          </g>
        ))}
      </svg>

      {/* hover tooltip */}
      {hover && (
        <div
          style={{
            position: 'absolute',
            top: 8,
            left: `${Math.min(hover.pctX, 72)}%`,
            marginLeft: 10,
            background: 'var(--surface)',
            border: '0.5px solid var(--border-2)',
            borderRadius: 8,
            boxShadow: 'var(--shadow-card)',
            padding: '7px 10px',
            pointerEvents: 'none',
            zIndex: 2,
          }}
        >
          <div className="mono" style={{ fontSize: 10, color: 'var(--text-3)', marginBottom: 4 }}>
            step {hover.step}
          </div>
          {hover.entries.map((en) => (
            <div key={en.name} className="row gap6" style={{ fontSize: 11, marginTop: 2 }}>
              <span style={{ width: 7, height: 7, borderRadius: '50%', background: en.color, flexShrink: 0 }} />
              <span style={{ color: 'var(--text-2)' }}>{en.name}</span>
              <span className="mono" style={{ fontWeight: 700 }}>{fmtVal(en.value)}</span>
            </div>
          ))}
        </div>
      )}

      {/* 图例 */}
      <div className="row gap12" style={{ flexWrap: 'wrap', marginTop: 8 }}>
        {data.map((s) => (
          <span key={s.name} className="row gap6" style={{ fontSize: 11, color: 'var(--text-2)' }}>
            <span style={{ width: 14, height: 2.5, borderRadius: 2, background: s.color, flexShrink: 0 }} />
            <span className="mono">{s.name}</span>
          </span>
        ))}
      </div>
    </div>
  );
}
