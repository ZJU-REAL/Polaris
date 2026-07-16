import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import { Segmented } from '../../components/ui/Segmented';
import { RelevanceBar } from '../../components/ui/RelevanceBar';
import { api, type GraphData, type GraphEdge, type GraphNode, type GraphNodeType } from '../../lib/api';
import { categoryMeta, SearchInput } from './shared';

/* ============================================================
   知识图谱 Tab：论文 / 作者 / 概念的可探查网络，三种视图：
   - 网络 Network：canvas 力导向（分层径向初始 + 碰撞防重叠），自由探索；
   - 时间线 Timeline：按发表年份分列的整齐排布；
   - 主题 Topics：按概念聚类的分组网格；
   - 趋势 Trends：概念演化冲积图（河流图），条带宽度 = 当期关联论文数。
   全部视图共享「子主题」过滤：选中一个概念后只看它牵出的论文子图。
   ============================================================ */

type GraphView = 'network' | 'timeline' | 'topics' | 'trends';

interface SimNode extends GraphNode {
  x: number;
  y: number;
  vx: number;
  vy: number;
  r: number;
  color: string;
}

const TYPE_META: { v: GraphNodeType; label: string; cssVar: string }[] = [
  { v: 'paper', label: '论文', cssVar: '--accent' },
  { v: 'concept', label: '概念', cssVar: '--ok' },
  { v: 'author', label: '作者', cssVar: '--warn' },
];

function cssColor(name: string, fallback: string): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function nodeRadius(n: GraphNode): number {
  if (n.type === 'paper') return 5 + (n.relevance ?? 0.5) * 3;
  return 3.5 + Math.min(6, Math.sqrt(n.count ?? 1) * 1.6);
}

function truncate(s: string, max: number): string {
  return s.length > max ? `${s.slice(0, max)}…` : s;
}

/* ---------------- 数据模型（含子主题过滤） ---------------- */

interface GraphModel {
  nodes: GraphNode[];
  edges: GraphEdge[];
  papers: GraphNode[];
  concepts: GraphNode[];
  /** paperId → 概念节点列表 */
  conceptsOfPaper: Map<string, GraphNode[]>;
  /** conceptId → 论文节点列表 */
  papersOfConcept: Map<string, GraphNode[]>;
}

function buildModel(data: GraphData, focusConceptId: string): GraphModel {
  const byId = new Map(data.nodes.map((n) => [n.id, n]));
  // 子主题过滤：只保留与选中概念相连的论文，及这些论文牵出的概念/作者
  let keep: Set<string> | null = null;
  if (focusConceptId) {
    const paperIds = new Set(
      data.edges
        .filter((e) => e.kind === 'paper_concept' && e.target === focusConceptId)
        .map((e) => e.source),
    );
    keep = new Set(paperIds);
    keep.add(focusConceptId);
    for (const e of data.edges) {
      if (paperIds.has(e.source)) keep.add(e.target);
    }
  }
  const nodes = keep ? data.nodes.filter((n) => keep.has(n.id)) : data.nodes;
  const kept = new Set(nodes.map((n) => n.id));
  const edges = data.edges.filter((e) => kept.has(e.source) && kept.has(e.target));

  const conceptsOfPaper = new Map<string, GraphNode[]>();
  const papersOfConcept = new Map<string, GraphNode[]>();
  for (const e of edges) {
    if (e.kind !== 'paper_concept') continue;
    const paper = byId.get(e.source);
    const concept = byId.get(e.target);
    if (!paper || !concept) continue;
    (conceptsOfPaper.get(e.source) ?? conceptsOfPaper.set(e.source, []).get(e.source)!).push(concept);
    (papersOfConcept.get(e.target) ?? papersOfConcept.set(e.target, []).get(e.target)!).push(paper);
  }
  return {
    nodes,
    edges,
    papers: nodes.filter((n) => n.type === 'paper'),
    concepts: nodes.filter((n) => n.type === 'concept'),
    conceptsOfPaper,
    papersOfConcept,
  };
}

/* ---------------- 网络视图（canvas 力导向） ---------------- */

function NetworkView({
  model,
  onOpenPaper,
  onOpenConcept,
  onFocusConcept,
}: {
  model: GraphModel;
  onOpenPaper: (id: string) => void;
  onOpenConcept: (id: string) => void;
  /** 「以此为子主题」：把选中概念设为全局过滤 */
  onFocusConcept: (id: string) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  const [hiddenTypes, setHiddenTypes] = useState<Set<GraphNodeType>>(new Set());
  const [q, setQ] = useState('');
  const [selected, setSelected] = useState<GraphNode | null>(null);
  const [hovered, setHovered] = useState<GraphNode | null>(null);

  const nodesRef = useRef<SimNode[]>([]);
  const edgesRef = useRef<GraphEdge[]>([]);
  const byIdRef = useRef<Map<string, SimNode>>(new Map());
  const neighborsRef = useRef<Map<string, Set<string>>>(new Map());
  const alphaRef = useRef(1);
  const viewRef = useRef({ tx: 0, ty: 0, scale: 1 });
  const rafRef = useRef(0);
  const hiddenRef = useRef(hiddenTypes);
  const qRef = useRef('');
  const selectedRef = useRef<string | null>(null);
  const hoveredRef = useRef<string | null>(null);
  const dragRef = useRef<{ kind: 'pan' | 'node'; id?: string; moved: boolean; lastX: number; lastY: number } | null>(null);

  hiddenRef.current = hiddenTypes;
  qRef.current = q.trim().toLowerCase();
  selectedRef.current = selected?.id ?? null;
  hoveredRef.current = hovered?.id ?? null;

  // —— 初始化：分层径向布局（概念内圈、论文中圈、作者外圈）→ 起点就相对整齐 ——
  useEffect(() => {
    const colors: Record<GraphNodeType, string> = {
      paper: cssColor('--accent', '#003f88'),
      concept: cssColor('--ok', '#3f8f5f'),
      author: cssColor('--warn', '#b4892f'),
    };
    const wrap = wrapRef.current;
    const w = wrap?.clientWidth ?? 800;
    const h = wrap?.clientHeight ?? 600;
    const base = Math.min(w, h);
    const ringOf: Record<GraphNodeType, number> = { concept: 0.16, paper: 0.34, author: 0.48 };
    const counters: Record<GraphNodeType, number> = { paper: 0, concept: 0, author: 0 };
    const totals: Record<GraphNodeType, number> = { paper: 0, concept: 0, author: 0 };
    for (const n of model.nodes) totals[n.type] += 1;

    const prev = byIdRef.current;
    const nodes: SimNode[] = model.nodes.map((n) => {
      const old = prev.get(n.id);
      const idx = counters[n.type]++;
      const angle = (idx / Math.max(1, totals[n.type])) * Math.PI * 2 + (n.type === 'paper' ? 0.4 : 0);
      const rad = base * ringOf[n.type] * (0.9 + ((idx * 13) % 10) / 45);
      return {
        ...n,
        x: old?.x ?? w / 2 + Math.cos(angle) * rad,
        y: old?.y ?? h / 2 + Math.sin(angle) * rad,
        vx: 0,
        vy: 0,
        r: nodeRadius(n),
        color: colors[n.type],
      };
    });
    nodesRef.current = nodes;
    edgesRef.current = model.edges;
    byIdRef.current = new Map(nodes.map((n) => [n.id, n]));
    const neighbors = new Map<string, Set<string>>();
    for (const e of model.edges) {
      (neighbors.get(e.source) ?? neighbors.set(e.source, new Set()).get(e.source)!).add(e.target);
      (neighbors.get(e.target) ?? neighbors.set(e.target, new Set()).get(e.target)!).add(e.source);
    }
    neighborsRef.current = neighbors;
    alphaRef.current = 1;
    setSelected(null);
  }, [model]);

  const visible = useCallback((n: SimNode) => !hiddenRef.current.has(n.type), []);

  // —— tick + 绘制 ——
  useEffect(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;

    const edgeColor = cssColor('--border-2', '#d6dce6');
    const textColor = cssColor('--text-2', '#4b5567');
    const haloColor = cssColor('--accent', '#003f88');

    const tick = () => {
      const nodes = nodesRef.current.filter(visible);
      const alpha = alphaRef.current;
      if (alpha > 0.02 && nodes.length > 0) {
        const cx = wrap.clientWidth / 2;
        const cy = wrap.clientHeight / 2;
        for (let i = 0; i < nodes.length; i++) {
          const a = nodes[i];
          if (!a) continue;
          for (let j = i + 1; j < nodes.length; j++) {
            const b = nodes[j];
            if (!b) continue;
            let dx = a.x - b.x;
            let dy = a.y - b.y;
            let d2 = dx * dx + dy * dy;
            if (d2 < 1) {
              dx = (Math.random() - 0.5) * 2;
              dy = (Math.random() - 0.5) * 2;
              d2 = dx * dx + dy * dy;
            }
            if (d2 > 90_000) continue;
            const d = Math.sqrt(d2);
            // 斥力 + 碰撞（重叠时强推开，画面更整齐）
            let f = (900 / d2) * alpha;
            const minD = a.r + b.r + 6;
            if (d < minD) f += ((minD - d) / d) * 0.35;
            const fx = dx * f;
            const fy = dy * f;
            a.vx += fx;
            a.vy += fy;
            b.vx -= fx;
            b.vy -= fy;
          }
          a.vx += (cx - a.x) * 0.012 * alpha;
          a.vy += (cy - a.y) * 0.012 * alpha;
        }
        for (const e of edgesRef.current) {
          const a = byIdRef.current.get(e.source);
          const b = byIdRef.current.get(e.target);
          if (!a || !b || !visible(a) || !visible(b)) continue;
          const dx = b.x - a.x;
          const dy = b.y - a.y;
          const d = Math.max(1, Math.sqrt(dx * dx + dy * dy));
          const rest = e.kind === 'paper_concept' ? 70 : 55;
          const f = ((d - rest) / d) * 0.05 * alpha;
          a.vx += dx * f;
          a.vy += dy * f;
          b.vx -= dx * f;
          b.vy -= dy * f;
        }
        for (const n of nodes) {
          if (dragRef.current?.kind === 'node' && dragRef.current.id === n.id) continue;
          n.vx *= 0.82;
          n.vy *= 0.82;
          n.x += n.vx;
          n.y += n.vy;
        }
        alphaRef.current = alpha * 0.995;
      }

      // html 有全局 zoom（global.css）：canvas 像素缓冲按 dpr×zoom 分配才不糊
      const zoom = parseFloat(getComputedStyle(document.documentElement).zoom || '1') || 1;
      const dpr = (window.devicePixelRatio || 1) * zoom;
      const w = wrap.clientWidth;
      const h = wrap.clientHeight;
      if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
        canvas.width = w * dpr;
        canvas.height = h * dpr;
        canvas.style.width = `${w}px`;
        canvas.style.height = `${h}px`;
      }
      const ctx = canvas.getContext('2d');
      if (!ctx) return;
      const { tx, ty, scale } = viewRef.current;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      ctx.setTransform(dpr * scale, 0, 0, dpr * scale, dpr * tx, dpr * ty);

      const focusId = hoveredRef.current ?? selectedRef.current;
      const focusSet = focusId ? new Set([focusId, ...(neighborsRef.current.get(focusId) ?? [])]) : null;
      const query = qRef.current;

      for (const e of edgesRef.current) {
        const a = byIdRef.current.get(e.source);
        const b = byIdRef.current.get(e.target);
        if (!a || !b || !visible(a) || !visible(b)) continue;
        const inFocus = focusId !== null && (e.source === focusId || e.target === focusId);
        ctx.strokeStyle = inFocus ? haloColor : edgeColor;
        ctx.globalAlpha = focusSet && !inFocus ? 0.12 : inFocus ? 0.8 : 0.45;
        ctx.lineWidth = (inFocus ? 1.4 : 0.8) / scale;
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
      }

      for (const n of nodesRef.current) {
        if (!visible(n)) continue;
        const matched = query.length > 0 && n.label.toLowerCase().includes(query);
        const dimmed = (focusSet && !focusSet.has(n.id)) || (query.length > 0 && !matched);
        ctx.globalAlpha = dimmed ? 0.18 : 1;
        ctx.fillStyle = n.color;
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.r / Math.sqrt(scale), 0, Math.PI * 2);
        ctx.fill();
        if (n.id === selectedRef.current || matched) {
          ctx.strokeStyle = haloColor;
          ctx.lineWidth = 1.6 / scale;
          ctx.stroke();
        }
        const important =
          n.id === focusId || matched || (n.type !== 'paper' && (n.count ?? 1) >= 3) || scale > 1.5;
        if (important && !dimmed && (scale > 0.5 || n.id === focusId)) {
          ctx.globalAlpha = 0.92;
          ctx.fillStyle = textColor;
          ctx.font = `${11 / scale}px sans-serif`;
          ctx.fillText(truncate(n.label, 26), n.x + (n.r + 3) / scale, n.y + 3 / scale);
        }
      }
      ctx.globalAlpha = 1;
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafRef.current);
  }, [visible]);

  const hitTest = useCallback((clientX: number, clientY: number): SimNode | null => {
    const canvas = canvasRef.current;
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
    const { tx, ty, scale } = viewRef.current;
    const x = (clientX - rect.left - tx) / scale;
    const y = (clientY - rect.top - ty) / scale;
    let best: SimNode | null = null;
    let bestD = Infinity;
    for (const n of nodesRef.current) {
      if (hiddenRef.current.has(n.type)) continue;
      const dx = n.x - x;
      const dy = n.y - y;
      const d = Math.sqrt(dx * dx + dy * dy);
      const hitR = n.r / Math.sqrt(scale) + 4 / scale;
      if (d < hitR && d < bestD) {
        best = n;
        bestD = d;
      }
    }
    return best;
  }, []);

  const onMouseDown = (e: React.MouseEvent) => {
    const hit = hitTest(e.clientX, e.clientY);
    dragRef.current = { kind: hit ? 'node' : 'pan', id: hit?.id, moved: false, lastX: e.clientX, lastY: e.clientY };
  };
  const onMouseMove = (e: React.MouseEvent) => {
    const drag = dragRef.current;
    if (!drag) {
      const hit = hitTest(e.clientX, e.clientY);
      setHovered((old) => (old?.id === hit?.id ? old : hit));
      return;
    }
    const dx = e.clientX - drag.lastX;
    const dy = e.clientY - drag.lastY;
    if (Math.abs(dx) + Math.abs(dy) > 2) drag.moved = true;
    drag.lastX = e.clientX;
    drag.lastY = e.clientY;
    if (drag.kind === 'pan') {
      viewRef.current.tx += dx;
      viewRef.current.ty += dy;
    } else if (drag.id) {
      const n = byIdRef.current.get(drag.id);
      if (n) {
        n.x += dx / viewRef.current.scale;
        n.y += dy / viewRef.current.scale;
        n.vx = 0;
        n.vy = 0;
        alphaRef.current = Math.max(alphaRef.current, 0.3);
      }
    }
  };
  const endDrag = (e: React.MouseEvent) => {
    const drag = dragRef.current;
    dragRef.current = null;
    if (!drag || drag.moved) return;
    const hit = hitTest(e.clientX, e.clientY);
    setSelected((old) => (hit && old?.id !== hit.id ? hit : null));
  };
  const onWheel = (e: React.WheelEvent) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const view = viewRef.current;
    const factor = Math.exp(-e.deltaY * 0.0015);
    const next = Math.min(4, Math.max(0.25, view.scale * factor));
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;
    view.tx = px - ((px - view.tx) / view.scale) * next;
    view.ty = py - ((py - view.ty) / view.scale) * next;
    view.scale = next;
  };

  const counts = useMemo(() => {
    const c: Record<GraphNodeType, number> = { paper: 0, concept: 0, author: 0 };
    for (const n of model.nodes) c[n.type] += 1;
    return c;
  }, [model]);

  return (
    <>
      <div className="row gap8 wrap" style={{ padding: '8px 14px', borderBottom: '0.5px solid var(--border)' }}>
        {TYPE_META.map((t) => {
          const off = hiddenTypes.has(t.v);
          return (
            <span
              key={t.v}
              className={`chip${off ? '' : ' on'}`}
              onClick={() =>
                setHiddenTypes((old) => {
                  const next = new Set(old);
                  if (next.has(t.v)) next.delete(t.v);
                  else next.add(t.v);
                  return next;
                })
              }
              title={off ? `显示${t.label}节点` : `隐藏${t.label}节点`}
            >
              <span
                style={{
                  display: 'inline-block',
                  width: 8,
                  height: 8,
                  borderRadius: '50%',
                  background: `var(${t.cssVar})`,
                  marginRight: 5,
                  opacity: off ? 0.3 : 1,
                }}
              />
              {t.label} {counts[t.v]}
            </span>
          );
        })}
        <div style={{ width: 190 }}>
          <SearchInput value={q} onChange={setQ} placeholder="搜索节点名称…" />
        </div>
        <button className="btn btn-ghost sm" onClick={() => (alphaRef.current = 1)} title="重新计算布局">
          <Icon name="refresh" size={13} />
          重新布局
        </button>
        <span className="mono muted" style={{ fontSize: 10.5, marginLeft: 'auto' }}>
          拖拽平移 · 滚轮缩放 · 点节点看关联
        </span>
      </div>

      <div ref={wrapRef} style={{ position: 'relative', flex: 1, minHeight: 0, overflow: 'hidden' }}>
        <canvas
          ref={canvasRef}
          style={{ display: 'block', cursor: hovered ? 'pointer' : 'grab' }}
          onMouseDown={onMouseDown}
          onMouseMove={onMouseMove}
          onMouseUp={endDrag}
          onMouseLeave={(e) => {
            endDrag(e);
            setHovered(null);
          }}
          onWheel={onWheel}
        />

        {hovered && !selected && (
          <div className="card" style={{ position: 'absolute', left: 14, bottom: 14, padding: '8px 12px', maxWidth: 380, pointerEvents: 'none' }}>
            <div style={{ fontSize: 12.5, fontWeight: 650, lineHeight: 1.4 }}>{hovered.label}</div>
            <div className="muted" style={{ fontSize: 11, marginTop: 2 }}>
              {TYPE_META.find((t) => t.v === hovered.type)?.label}
              {hovered.type === 'paper' && hovered.year ? ` · ${hovered.year}` : ''}
              {hovered.type !== 'paper' ? ` · 关联 ${hovered.count ?? 1} 篇论文` : ''}
            </div>
          </div>
        )}

        {selected && (
          <div className="card" style={{ position: 'absolute', left: 14, bottom: 14, padding: '10px 14px', maxWidth: 400 }}>
            <div className="row gap8" style={{ alignItems: 'flex-start' }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13, fontWeight: 650, lineHeight: 1.4 }}>{selected.label}</div>
                <div className="muted" style={{ fontSize: 11, marginTop: 3 }}>
                  {TYPE_META.find((t) => t.v === selected.type)?.label}
                  {selected.type === 'paper' && selected.year ? ` · ${selected.year}` : ''}
                  {selected.type === 'paper' && typeof selected.relevance === 'number'
                    ? ` · 相关度 ${(selected.relevance * 10).toFixed(1)}`
                    : ''}
                  {selected.type !== 'paper' ? ` · 关联 ${selected.count ?? 1} 篇论文` : ''}
                </div>
              </div>
              <button className="icon-btn" style={{ width: 22, height: 22, border: 'none', background: 'transparent' }} onClick={() => setSelected(null)}>
                <Icon name="x" size={12} />
              </button>
            </div>
            <div className="row gap6 wrap" style={{ marginTop: 8 }}>
              {selected.type !== 'author' && (
                <button
                  className="btn btn-soft sm"
                  onClick={() => (selected.type === 'paper' ? onOpenPaper(selected.id) : onOpenConcept(selected.id))}
                >
                  <Icon name={selected.type === 'paper' ? 'book' : 'layers'} size={12} />
                  打开{selected.type === 'paper' ? '论文' : '概念'}详情
                </button>
              )}
              {selected.type === 'concept' && (
                <button className="btn btn-ghost sm" onClick={() => onFocusConcept(selected.id)}>
                  <Icon name="search" size={12} />
                  只看这个子主题
                </button>
              )}
            </div>
          </div>
        )}
      </div>
    </>
  );
}

/* ---------------- 时间线视图（按年份分列，DOM 排布） ---------------- */

function PaperMiniCard({
  p,
  concepts,
  onOpenPaper,
}: {
  p: GraphNode;
  concepts: GraphNode[];
  onOpenPaper: (id: string) => void;
}) {
  return (
    <div
      className="card"
      style={{ padding: '8px 10px', cursor: 'pointer', border: '0.5px solid var(--border)' }}
      title={p.label}
      onClick={() => onOpenPaper(p.id)}
    >
      <div
        style={{
          fontSize: 11.5,
          fontWeight: 600,
          lineHeight: 1.4,
          display: '-webkit-box',
          WebkitLineClamp: 2,
          WebkitBoxOrient: 'vertical',
          overflow: 'hidden',
        }}
      >
        {p.label}
      </div>
      <div className="row gap6" style={{ marginTop: 5 }}>
        {typeof p.relevance === 'number' && <RelevanceBar value={p.relevance} width={46} />}
        <span className="mono" style={{ fontSize: 9.5, color: 'var(--text-4)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {concepts.slice(0, 2).map((c) => c.label).join(' · ')}
        </span>
      </div>
    </div>
  );
}

type TimelineGranularity = 'month' | 'year';

function TimelineView({ model, onOpenPaper }: { model: GraphModel; onOpenPaper: (id: string) => void }) {
  const [granularity, setGranularity] = useState<TimelineGranularity>('month');
  const columns = useMemo(() => {
    const byPeriod = new Map<string, GraphNode[]>();
    for (const p of model.papers) {
      // 按月优先用精确发表日期；缺失时退回年份（列头显示为整年）
      const key =
        granularity === 'month'
          ? (p.published?.slice(0, 7) ?? (p.year ? String(p.year) : '时间未知'))
          : (p.year ? String(p.year) : (p.published?.slice(0, 4) ?? '时间未知'));
      (byPeriod.get(key) ?? byPeriod.set(key, []).get(key)!).push(p);
    }
    // 'YYYY' 与 'YYYY-MM' 混排时字典序即时间序
    const keys = [...byPeriod.keys()].sort((a, b) => {
      if (a === '时间未知') return 1;
      if (b === '时间未知') return -1;
      return a.localeCompare(b);
    });
    return keys.map((k) => ({
      year: k,
      papers: (byPeriod.get(k) ?? []).sort((a, b) => (b.relevance ?? 0) - (a.relevance ?? 0)),
    }));
  }, [model, granularity]);

  if (columns.length === 0) {
    return <div className="empty" style={{ margin: 'auto' }}>当前筛选下没有论文</div>;
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
      <div className="row gap8" style={{ padding: '10px 16px 0', flexShrink: 0 }}>
        <Segmented<TimelineGranularity>
          options={[
            { v: 'month', label: '按月份' },
            { v: 'year', label: '按年份' },
          ]}
          value={granularity}
          onChange={setGranularity}
        />
      </div>
    <div className="scroll" style={{ flex: 1, minHeight: 0, overflowX: 'auto', overflowY: 'hidden', padding: '14px 16px' }}>
      <div className="row" style={{ gap: 14, alignItems: 'stretch', height: '100%' }}>
        {columns.map((col) => (
          <div key={col.year} style={{ width: 210, flexShrink: 0, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
            <div
              className="row gap8"
              style={{ paddingBottom: 8, marginBottom: 10, borderBottom: '2px solid var(--accent-soft-2)', flexShrink: 0 }}
            >
              <span className="mono" style={{ fontSize: 14, fontWeight: 700, color: 'var(--accent-text)' }}>{col.year}</span>
              <span className="mono muted" style={{ fontSize: 10.5 }}>{col.papers.length} 篇</span>
            </div>
            <div className="col scroll" style={{ gap: 8, overflowY: 'auto', minHeight: 0, paddingBottom: 8 }}>
              {col.papers.map((p) => (
                <PaperMiniCard key={p.id} p={p} concepts={model.conceptsOfPaper.get(p.id) ?? []} onOpenPaper={onOpenPaper} />
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
    </div>
  );
}

/* ---------------- 趋势视图（概念演化冲积图 / 河流图） ---------------- */

const TREND_BAND_LIMIT = 12;

// 概念类别 → 基准色相/饱和度（HSL）；同类别的条带用明度错开
const TREND_HUE: Record<string, [number, number]> = {
  method: [214, 60],
  architecture: [199, 58],
  methodology: [262, 46],
  problem: [4, 56],
  metric: [152, 40],
  dataset: [40, 64],
  other: [222, 12],
};

const TREND_HUE_FALLBACK: [number, number] = [222, 12];

function trendColor(category: string | null | undefined, seq: number): string {
  const [h, sat] = TREND_HUE[category ?? 'other'] ?? TREND_HUE_FALLBACK;
  return `hsl(${h} ${sat}% ${50 + (seq % 3) * 9}%)`;
}

/** 论文归属的时间桶；缺发表日期用年份兜底（按月粒度归到年中），都没有返回 null 不计入。 */
function paperPeriod(p: GraphNode, granularity: TimelineGranularity): string | null {
  if (granularity === 'month') {
    if (p.published) return p.published.slice(0, 7);
    return p.year ? `${p.year}-06` : null;
  }
  if (p.year) return String(p.year);
  return p.published?.slice(0, 4) ?? null;
}

/** [min, max] 间的连续时间桶（补齐中间的空桶）。 */
function periodRange(min: string, max: string, granularity: TimelineGranularity): string[] {
  const out: string[] = [];
  if (granularity === 'year') {
    for (let y = Number(min); y <= Number(max); y += 1) out.push(String(y));
    return out;
  }
  let y = Number(min.slice(0, 4));
  let m = Number(min.slice(5, 7));
  const endY = Number(max.slice(0, 4));
  const endM = Number(max.slice(5, 7));
  while (y < endY || (y === endY && m <= endM)) {
    out.push(`${y}-${String(m).padStart(2, '0')}`);
    m += 1;
    if (m > 12) {
      m = 1;
      y += 1;
    }
  }
  return out;
}

/** Catmull-Rom 过点平滑 → 贝塞尔 C 段（调用前需已 M/L 到 pts[0]）。 */
function smoothSegments(pts: { x: number; y: number }[]): string {
  let d = '';
  for (let i = 0; i < pts.length - 1; i += 1) {
    const p0 = pts[Math.max(0, i - 1)]!;
    const p1 = pts[i]!;
    const p2 = pts[i + 1]!;
    const p3 = pts[Math.min(pts.length - 1, i + 2)]!;
    const c1x = p1.x + (p2.x - p0.x) / 6;
    const c1y = p1.y + (p2.y - p0.y) / 6;
    const c2x = p2.x - (p3.x - p1.x) / 6;
    const c2y = p2.y - (p3.y - p1.y) / 6;
    d += ` C ${c1x.toFixed(1)} ${c1y.toFixed(1)}, ${c2x.toFixed(1)} ${c2y.toFixed(1)}, ${p2.x.toFixed(1)} ${p2.y.toFixed(1)}`;
  }
  return d;
}

interface TrendBand {
  id: string; // conceptId；合并的长尾条带为 '__other__'
  label: string;
  category: string | null | undefined;
  raw: number[]; // 原始每期论文数（tooltip 显示用）
  values: number[]; // 平滑后的绘图值
  color: string;
}

function TrendsView({ model, onFocusConcept }: { model: GraphModel; onFocusConcept: (id: string) => void }) {
  const [granularity, setGranularity] = useState<TimelineGranularity>('month');
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [hover, setHover] = useState<{ band: TrendBand; ti: number; x: number; y: number } | null>(null);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const data = useMemo(() => {
    // —— 概念 × 时间桶 → 关联论文数矩阵 ——
    const periodOfPaper = new Map<string, string | null>();
    let unknown = 0;
    for (const p of model.papers) {
      const key = paperPeriod(p, granularity);
      periodOfPaper.set(p.id, key);
      if (key === null) unknown += 1;
    }
    const perConcept = new Map<string, Map<string, number>>();
    const totals = new Map<string, number>();
    for (const c of model.concepts) {
      const counts = new Map<string, number>();
      let total = 0;
      for (const p of model.papersOfConcept.get(c.id) ?? []) {
        const key = periodOfPaper.get(p.id);
        if (!key) continue;
        counts.set(key, (counts.get(key) ?? 0) + 1);
        total += 1;
      }
      if (total > 0) {
        perConcept.set(c.id, counts);
        totals.set(c.id, total);
      }
    }
    const allKeys = [...perConcept.values()].flatMap((m) => [...m.keys()]).sort();
    if (allKeys.length === 0) return { bands: [] as TrendBand[], periods: [] as string[], unknown };
    const periods = periodRange(allKeys[0]!, allKeys[allKeys.length - 1]!, granularity);

    const byId = new Map(model.concepts.map((c) => [c.id, c]));
    const catSeq = new Map<string, number>();
    const mkBand = (
      id: string,
      label: string,
      category: string | null | undefined,
      counts: Map<string, number>,
    ): TrendBand => {
      const raw = periods.map((k) => counts.get(k) ?? 0);
      // 月度桶稀疏，1-2-1 加权平滑只影响形状；tooltip 仍显示原始数
      const values =
        granularity === 'month'
          ? raw.map((v, i) => (raw[i - 1] ?? 0) * 0.25 + v * 0.5 + (raw[i + 1] ?? 0) * 0.25)
          : [...raw];
      const seq = catSeq.get(category ?? 'other') ?? 0;
      catSeq.set(category ?? 'other', seq + 1);
      return { id, label, category, raw, values, color: trendColor(category, seq) };
    };
    const ranked = [...totals.keys()].sort((a, b) => totals.get(b)! - totals.get(a)!);
    const bandsRaw = ranked.slice(0, TREND_BAND_LIMIT).map((id) => {
      const c = byId.get(id)!;
      return mkBand(id, c.label, c.category, perConcept.get(id)!);
    });
    const restIds = ranked.slice(TREND_BAND_LIMIT);
    if (restIds.length > 0) {
      const merged = new Map<string, number>();
      for (const id of restIds) {
        for (const [k, v] of perConcept.get(id)!) merged.set(k, (merged.get(k) ?? 0) + v);
      }
      bandsRaw.push({
        ...mkBand('__other__', `其他 ${restIds.length} 个概念`, 'other', merged),
        color: 'hsl(222 8% 78%)',
      });
    }
    // inside-out：按峰值时间排序后从中心向两侧交替放，条带汇聚分流更可读
    const peakAt = (b: TrendBand) => b.values.indexOf(Math.max(...b.values));
    const byPeak = [...bandsRaw].sort((a, b) => peakAt(a) - peakAt(b));
    const upper: TrendBand[] = [];
    const lower: TrendBand[] = [];
    byPeak.forEach((b, i) => (i % 2 === 0 ? upper : lower).push(b));
    return { bands: [...upper.reverse(), ...lower], periods, unknown };
  }, [model, granularity]);

  // —— 几何：silhouette 基线（每期条带围绕中轴对称堆叠） ——
  const geom = useMemo(() => {
    const { bands, periods } = data;
    const { w, h } = size;
    if (!w || !h || bands.length === 0 || periods.length < 2) return null;
    const mL = 14;
    const mR = 14;
    const mT = 10;
    const mB = 24;
    const n = periods.length;
    const xs = periods.map((_, i) => mL + ((w - mL - mR) * i) / (n - 1));
    let maxTotal = 0;
    for (let i = 0; i < n; i += 1) {
      maxTotal = Math.max(maxTotal, bands.reduce((sum, b) => sum + (b.values[i] ?? 0), 0));
    }
    if (maxTotal === 0) return null;
    const yScale = (h - mT - mB) / maxTotal;
    const mid = mT + (h - mT - mB) / 2;
    const layers = bands.map(() => ({ top: [] as { x: number; y: number }[], bot: [] as { x: number; y: number }[] }));
    for (let i = 0; i < n; i += 1) {
      const total = bands.reduce((sum, b) => sum + (b.values[i] ?? 0), 0);
      const x = xs[i]!;
      let y = mid - (total * yScale) / 2;
      bands.forEach((b, bi) => {
        layers[bi]!.top.push({ x, y });
        y += (b.values[i] ?? 0) * yScale;
        layers[bi]!.bot.push({ x, y });
      });
    }
    const paths = layers.map(({ top, bot }) => {
      const rb = [...bot].reverse();
      const t0 = top[0]!;
      const b0 = rb[0]!;
      return (
        `M ${t0.x.toFixed(1)} ${t0.y.toFixed(1)}` +
        smoothSegments(top) +
        ` L ${b0.x.toFixed(1)} ${b0.y.toFixed(1)}` +
        smoothSegments(rb) +
        ' Z'
      );
    });
    const step = Math.max(1, Math.ceil(n / 8));
    const ticks = periods
      .map((label, i) => ({ label, x: xs[i] ?? 0 }))
      .filter((_, i) => i % step === 0 || i === n - 1);
    return { paths, xs, ticks, mid, mB };
  }, [data, size]);

  const { bands, periods, unknown } = data;
  const enough = bands.length >= 1 && periods.length >= 2;

  const handleMove = (band: TrendBand) => (e: React.MouseEvent) => {
    const svg = svgRef.current;
    if (!svg || !geom) return;
    const rect = svg.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    let ti = 0;
    for (let i = 1; i < geom.xs.length; i += 1) {
      if (Math.abs((geom.xs[i] ?? 0) - x) < Math.abs((geom.xs[ti] ?? 0) - x)) ti = i;
    }
    setHover({ band, ti, x, y });
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
      <div className="row gap8 wrap" style={{ padding: '10px 16px 6px', flexShrink: 0 }}>
        <Segmented<TimelineGranularity>
          options={[
            { v: 'month', label: '按月份' },
            { v: 'year', label: '按年份' },
          ]}
          value={granularity}
          onChange={setGranularity}
        />
        <span className="mono muted" style={{ fontSize: 10.5, marginLeft: 'auto' }}>
          条带宽度 = 当期关联论文数
          {unknown > 0 ? ` · ${unknown} 篇缺发表时间未计入` : ''}
        </span>
      </div>
      {enough && (
        <div className="row gap6 wrap" style={{ padding: '0 16px 8px', flexShrink: 0 }}>
          {bands.map((b) => (
            <span
              key={b.id}
              className="pill sm"
              title={b.id === '__other__' ? undefined : '只看这个子主题'}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 5,
                background: 'var(--surface-2)',
                color: 'var(--text-2)',
                opacity: hover && hover.band.id !== b.id ? 0.45 : 1,
                cursor: b.id === '__other__' ? 'default' : 'pointer',
                transition: 'opacity 0.15s',
              }}
              onClick={() => b.id !== '__other__' && onFocusConcept(b.id)}
            >
              <span style={{ width: 8, height: 8, borderRadius: 2, background: b.color, flexShrink: 0 }} />
              {truncate(b.label, 16)}
            </span>
          ))}
        </div>
      )}
      <div ref={wrapRef} style={{ flex: 1, minHeight: 0, position: 'relative' }}>
        {!enough ? (
          <div className="empty" style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            编译并关联概念的论文还不够，暂时画不出趋势——至少需要覆盖两个时间点
          </div>
        ) : (
          geom && (
            <>
              <svg ref={svgRef} width={size.w} height={size.h} style={{ display: 'block' }} onMouseLeave={() => setHover(null)}>
                <line x1={0} x2={size.w} y1={geom.mid} y2={geom.mid} stroke="var(--border)" strokeWidth={0.5} strokeDasharray="3 5" />
                {geom.paths.map((d, i) => {
                  const band = bands[i]!;
                  return (
                    <path
                      key={band.id}
                      d={d}
                      fill={band.color}
                      fillOpacity={hover ? (hover.band.id === band.id ? 0.95 : 0.25) : 0.82}
                      stroke="var(--surface)"
                      strokeWidth={0.8}
                      style={{ cursor: band.id === '__other__' ? 'default' : 'pointer', transition: 'fill-opacity 0.15s' }}
                      onMouseMove={handleMove(band)}
                      onClick={() => band.id !== '__other__' && onFocusConcept(band.id)}
                    />
                  );
                })}
                {hover && (
                  <line
                    x1={geom.xs[hover.ti] ?? 0}
                    x2={geom.xs[hover.ti] ?? 0}
                    y1={8}
                    y2={size.h - geom.mB}
                    stroke="var(--text-3)"
                    strokeWidth={0.6}
                    strokeDasharray="2 3"
                  />
                )}
                {geom.ticks.map((t) => (
                  <text key={t.label} x={t.x} y={size.h - 8} textAnchor="middle" fontSize={9.5} fill="var(--text-4)" fontFamily="var(--mono)">
                    {t.label}
                  </text>
                ))}
              </svg>
              {hover && (
                <div
                  className="card"
                  style={{
                    position: 'absolute',
                    left: Math.max(4, Math.min(hover.x + 12, size.w - 175)),
                    top: Math.max(6, hover.y - 48),
                    pointerEvents: 'none',
                    padding: '7px 10px',
                    boxShadow: 'var(--shadow-pop)',
                    zIndex: 10,
                  }}
                >
                  <div className="row gap6" style={{ fontSize: 11.5, fontWeight: 650 }}>
                    <span style={{ width: 8, height: 8, borderRadius: 2, background: hover.band.color, flexShrink: 0 }} />
                    {truncate(hover.band.label, 26)}
                  </div>
                  <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)', marginTop: 2 }}>
                    {periods[hover.ti]} · {hover.band.raw[hover.ti]} 篇
                  </div>
                </div>
              )}
            </>
          )
        )}
      </div>
    </div>
  );
}

/* ---------------- 主题视图（按概念聚类分组） ---------------- */

const TOPIC_LIMIT = 12;

function TopicsView({
  model,
  onOpenPaper,
  onOpenConcept,
  onFocusConcept,
}: {
  model: GraphModel;
  onOpenPaper: (id: string) => void;
  onOpenConcept: (id: string) => void;
  onFocusConcept: (id: string) => void;
}) {
  const topics = useMemo(() => {
    const list = model.concepts
      .map((c) => ({ concept: c, papers: model.papersOfConcept.get(c.id) ?? [] }))
      .filter((t) => t.papers.length > 0)
      .sort((a, b) => b.papers.length - a.papers.length)
      .slice(0, TOPIC_LIMIT);
    for (const t of list) t.papers = [...t.papers].sort((a, b) => (b.relevance ?? 0) - (a.relevance ?? 0));
    // 没挂上任何概念的论文（还没编译/上链）归入「未归类」
    const covered = new Set(list.flatMap((t) => t.papers.map((p) => p.id)));
    const uncovered = model.papers.filter((p) => !covered.has(p.id) && (model.conceptsOfPaper.get(p.id) ?? []).length === 0);
    return { list, uncovered };
  }, [model]);

  if (topics.list.length === 0 && topics.uncovered.length === 0) {
    return <div className="empty" style={{ margin: 'auto' }}>当前筛选下没有可聚类的论文</div>;
  }
  return (
    <div className="scroll" style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '14px 16px' }}>
      <div className="col" style={{ gap: 18 }}>
        {topics.list.map(({ concept, papers }) => {
          const meta = categoryMeta(concept.category ?? 'other');
          return (
            <div key={concept.id}>
              <div className="row gap8" style={{ marginBottom: 8 }}>
                <span
                  className="wikilink"
                  style={{ background: meta.bg, color: meta.c, height: 24, cursor: 'pointer' }}
                  title="打开概念详情"
                  onClick={() => onOpenConcept(concept.id)}
                >
                  {concept.label}
                  <span style={{ opacity: 0.6, marginLeft: 5, fontSize: '0.85em' }}>{meta.zh}</span>
                </span>
                <span className="mono muted" style={{ fontSize: 10.5 }}>{papers.length} 篇</span>
                <button className="btn btn-ghost sm" style={{ height: 22, fontSize: 10.5 }} onClick={() => onFocusConcept(concept.id)}>
                  <Icon name="search" size={11} />
                  只看这个子主题
                </button>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: 8 }}>
                {papers.map((p) => (
                  <PaperMiniCard key={p.id} p={p} concepts={model.conceptsOfPaper.get(p.id) ?? []} onOpenPaper={onOpenPaper} />
                ))}
              </div>
            </div>
          );
        })}
        {topics.uncovered.length > 0 && (
          <div>
            <div className="row gap8" style={{ marginBottom: 8 }}>
              <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-3)' }}>未归类</span>
              <span className="mono muted" style={{ fontSize: 10.5 }}>
                {topics.uncovered.length} 篇（尚未编译，暂无概念关联）
              </span>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: 8 }}>
              {topics.uncovered.map((p) => (
                <PaperMiniCard key={p.id} p={p} concepts={[]} onOpenPaper={onOpenPaper} />
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/* ---------------- Tab 主体 ---------------- */

export interface GraphTabProps {
  pid: string;
  onOpenPaper: (id: string) => void;
  onOpenConcept: (id: string) => void;
}

export function GraphTab({ pid, onOpenPaper, onOpenConcept }: GraphTabProps) {
  const [view, setView] = useState<GraphView>('timeline');
  const [focusConceptId, setFocusConceptId] = useState('');

  const { data, isLoading, isError } = useQuery({
    queryKey: ['project-graph', pid],
    queryFn: () => api.getProjectGraph(pid),
    retry: false,
    staleTime: 60_000,
  });

  // 切方向时清空子主题
  useEffect(() => setFocusConceptId(''), [pid]);

  const model = useMemo(() => (data ? buildModel(data, focusConceptId) : null), [data, focusConceptId]);

  // 子主题下拉：全部概念按关联论文数排序
  const conceptOptions = useMemo(() => {
    if (!data) return [];
    return data.nodes
      .filter((n) => n.type === 'concept')
      .sort((a, b) => (b.count ?? 0) - (a.count ?? 0))
      .slice(0, 60);
  }, [data]);

  if (isLoading) return <div className="empty" style={{ margin: 'auto' }}>正在构建图谱…</div>;
  if (isError || !data) {
    return (
      <div style={{ margin: 'auto' }}>
        <EmptyState compact icon="x" title="无法加载图谱" desc="后端不可用或接口尚未就绪，稍后重试。" />
      </div>
    );
  }
  if (data.nodes.length === 0) {
    return (
      <div style={{ margin: 'auto' }}>
        <EmptyState
          compact
          icon="layers"
          title="还没有可展示的网络"
          desc="先运行初始建库让论文通过筛选并完成编译，图谱会自动把论文、作者、概念连成网络。"
        />
      </div>
    );
  }

  const focusConcept = focusConceptId ? data.nodes.find((n) => n.id === focusConceptId) : null;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
      {/* —— 视图 + 子主题工具栏 —— */}
      <div className="row gap8 wrap" style={{ padding: '10px 14px', borderBottom: '0.5px solid var(--border)' }}>
        <Segmented<GraphView>
          options={[
            { v: 'network', label: '网络' },
            { v: 'timeline', label: '时间线' },
            { v: 'trends', label: '趋势' },
            { v: 'topics', label: '主题' },
          ]}
          value={view}
          onChange={setView}
        />
        <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)', marginLeft: 6 }}>子主题</span>
        <select
          className="input"
          style={{ height: 28, fontSize: 12, maxWidth: 220, padding: '0 8px' }}
          value={focusConceptId}
          onChange={(e) => setFocusConceptId(e.target.value)}
          title="选一个概念，只看它牵出的论文子图"
        >
          <option value="">全部主题</option>
          {conceptOptions.map((c) => (
            <option key={c.id} value={c.id}>
              {c.label}（{c.count ?? 0}）
            </option>
          ))}
        </select>
        {focusConcept && (
          <span className="pill sm hoverable" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }} onClick={() => setFocusConceptId('')}>
            {focusConcept.label} ✕
          </span>
        )}
        <span className="mono muted" style={{ fontSize: 10.5, marginLeft: 'auto' }}>
          {data.truncated ? '论文较多，已按相关度展示前一批 · ' : ''}
          {model?.papers.length ?? 0} 篇论文 · {model?.concepts.length ?? 0} 个概念
        </span>
      </div>

      {model && view === 'network' && (
        <NetworkView model={model} onOpenPaper={onOpenPaper} onOpenConcept={onOpenConcept} onFocusConcept={setFocusConceptId} />
      )}
      {model && view === 'timeline' && <TimelineView model={model} onOpenPaper={onOpenPaper} />}
      {model && view === 'trends' && <TrendsView model={model} onFocusConcept={setFocusConceptId} />}
      {model && view === 'topics' && (
        <TopicsView model={model} onOpenPaper={onOpenPaper} onOpenConcept={onOpenConcept} onFocusConcept={setFocusConceptId} />
      )}
    </div>
  );
}
