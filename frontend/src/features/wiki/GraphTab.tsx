import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import { Segmented } from '../../components/ui/Segmented';
import { RelevanceBar } from '../../components/ui/RelevanceBar';
import { api, type GraphData, type GraphEdge, type GraphNode, type GraphNodeType } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { categoryMeta, SearchInput } from './shared';

/* ============================================================
   知识图谱 Tab：论文 / 作者 / 概念的可探查网络，三种视图：
   - 网络 Network：canvas 力导向（分层径向初始 + 碰撞防重叠），自由探索；
   - 时间线 Timeline：按发表年份分列的整齐排布；
   - 主题 Topics：按概念聚类的分组网格；
   - 趋势 Trends：概念演化冲积图（alluvial），每期一列节点块按数量排名
     堆叠、相邻列间连飘带，块高 = 当期关联论文数。
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

// 模块级常量存 zh/en 两份文案，渲染处再 tr（import 时求值不会随语言切换更新）
const TYPE_META: { v: GraphNodeType; zh: string; en: string; cssVar: string }[] = [
  { v: 'paper', zh: '论文', en: 'Paper', cssVar: '--accent' },
  { v: 'concept', zh: '概念', en: 'Concept', cssVar: '--ok' },
  { v: 'author', zh: '作者', en: 'Author', cssVar: '--warn' },
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
              title={
                off
                  ? tr(`显示${t.zh}节点`, `Show ${t.en.toLowerCase()} nodes`)
                  : tr(`隐藏${t.zh}节点`, `Hide ${t.en.toLowerCase()} nodes`)
              }
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
              {tr(t.zh, t.en)} {counts[t.v]}
            </span>
          );
        })}
        <div style={{ width: 190 }}>
          <SearchInput value={q} onChange={setQ} placeholder={tr('搜索节点名称…', 'Search nodes…')} />
        </div>
        <button
          className="btn btn-ghost sm"
          onClick={() => (alphaRef.current = 1)}
          title={tr('重新计算布局', 'Recompute the layout')}
        >
          <Icon name="refresh" size={13} />
          {tr('重新布局', 'Re-layout')}
        </button>
        <span className="mono muted" style={{ fontSize: 10.5, marginLeft: 'auto' }}>
          {tr('拖拽平移 · 滚轮缩放 · 点节点看关联', 'Drag to pan · scroll to zoom · click a node for links')}
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
              {(() => {
                const t = TYPE_META.find((m) => m.v === hovered.type);
                return t ? tr(t.zh, t.en) : '';
              })()}
              {hovered.type === 'paper' && hovered.year ? ` · ${hovered.year}` : ''}
              {hovered.type !== 'paper'
                ? tr(` · 关联 ${hovered.count ?? 1} 篇论文`, ` · linked to ${hovered.count ?? 1} papers`)
                : ''}
            </div>
          </div>
        )}

        {selected && (
          <div className="card" style={{ position: 'absolute', left: 14, bottom: 14, padding: '10px 14px', maxWidth: 400 }}>
            <div className="row gap8" style={{ alignItems: 'flex-start' }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13, fontWeight: 650, lineHeight: 1.4 }}>{selected.label}</div>
                <div className="muted" style={{ fontSize: 11, marginTop: 3 }}>
                  {(() => {
                    const t = TYPE_META.find((m) => m.v === selected.type);
                    return t ? tr(t.zh, t.en) : '';
                  })()}
                  {selected.type === 'paper' && selected.year ? ` · ${selected.year}` : ''}
                  {selected.type === 'paper' && typeof selected.relevance === 'number'
                    ? ` · ${tr('相关度', 'relevance')} ${(selected.relevance * 10).toFixed(1)}`
                    : ''}
                  {selected.type !== 'paper'
                    ? tr(` · 关联 ${selected.count ?? 1} 篇论文`, ` · linked to ${selected.count ?? 1} papers`)
                    : ''}
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
                  {selected.type === 'paper'
                    ? tr('打开论文详情', 'Open paper detail')
                    : tr('打开概念详情', 'Open concept detail')}
                </button>
              )}
              {selected.type === 'concept' && (
                <button className="btn btn-ghost sm" onClick={() => onFocusConcept(selected.id)}>
                  <Icon name="search" size={12} />
                  {tr('只看这个子主题', 'Focus on this subtopic')}
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
    return <div className="empty" style={{ margin: 'auto' }}>{tr('当前筛选下没有论文', 'No papers under the current filter')}</div>;
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
      <div className="row gap8" style={{ padding: '10px 16px 0', flexShrink: 0 }}>
        <Segmented<TimelineGranularity>
          options={[
            { v: 'month', label: tr('按月份', 'By month') },
            { v: 'year', label: tr('按年份', 'By year') },
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
              <span className="mono" style={{ fontSize: 14, fontWeight: 700, color: 'var(--accent-text)' }}>
                {col.year === '时间未知' ? tr('时间未知', 'Unknown date') : col.year}
              </span>
              <span className="mono muted" style={{ fontSize: 10.5 }}>{col.papers.length} {tr('篇', 'papers')}</span>
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

/* ---------------- 趋势视图（概念演化冲积图） ---------------- */

const TREND_BAND_LIMIT = 10;

// 固定顺序分类色板（按条带总量排名依次取色，一个概念一个专属色，不按类别复用色相
// ——top 概念常集中在同一类别，按类别配色会撞色）。
// 10 色已过 dataviz 校验（明度带 / 色度下限 / CVD 相邻区分）；
// 黄色对浅底对比不足 3:1，靠图例 + 悬停提示 + 末列直接标注兜底。
const TREND_PALETTE = [
  'hsl(214 62% 46%)',
  'hsl(40 64% 50%)',
  'hsl(262 46% 52%)',
  'hsl(152 42% 42%)',
  'hsl(222 48% 62%)',
  'hsl(4 56% 52%)',
  'hsl(199 72% 42%)',
  'hsl(80 45% 40%)',
  'hsl(330 50% 55%)',
  'hsl(25 60% 48%)',
];

/** 论文归属的时间桶；缺发表日期用年份兜底（按月粒度归到年中），都没有返回 null 不计入。 */
function paperPeriod(p: GraphNode, granularity: TimelineGranularity): string | null {
  if (granularity === 'month') {
    if (p.published) return p.published.slice(0, 7);
    return p.year ? `${p.year}-06` : null;
  }
  if (p.year) return String(p.year);
  return p.published?.slice(0, 4) ?? null;
}

interface TrendBand {
  id: string; // conceptId
  label: string;
  category: string | null | undefined;
  values: number[]; // 每期关联论文数（原始计数，冲积图不平滑）
  color: string;
}

/** 概念在某一期的节点块矩形。 */
interface TrendBlock {
  x: number;
  y0: number;
  y1: number;
}

const TREND_NODE_W = 10; // 节点块宽
const TREND_GAP = 3; // 同列块间留白（代替描边做分隔）
const TREND_MIN_H = 3; // 计数 = 1 也要看得见
const TREND_MAX_UNIT = 26; // 单篇论文的最大像素高：数据稀疏时块不至于撑满全高

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
    // —— 概念 × 时间桶 → 关联论文数矩阵；只保留有论文的时间桶（离散列，跳过空档） ——
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
    // 长尾概念不进图（几十个单次出现的概念会汇成巨大的灰带、压扁主角），只注记数量；
    // 时间桶也只从入选概念取，避免出现只有长尾的空列
    const ranked = [...totals.keys()].sort((a, b) => totals.get(b)! - totals.get(a)!);
    const top = ranked.slice(0, TREND_BAND_LIMIT);
    const periods = [...new Set(top.flatMap((id) => [...perConcept.get(id)!.keys()]))].sort();
    if (periods.length === 0) {
      return { bands: [] as TrendBand[], periods: [] as string[], unknown, rest: 0 };
    }

    const byId = new Map(model.concepts.map((c) => [c.id, c]));
    const bands = top.map((id, rank) => {
      const c = byId.get(id)!;
      const counts = perConcept.get(id)!;
      return {
        id,
        label: c.label,
        category: c.category,
        values: periods.map((k) => counts.get(k) ?? 0),
        color: TREND_PALETTE[rank]!,
      };
    });
    return { bands, periods, unknown, rest: ranked.length - top.length };
  }, [model, granularity]);

  // —— 冲积图几何：每列独立按当期数量排名堆叠，相邻列间连飘带 ——
  const geom = useMemo(() => {
    const { bands, periods } = data;
    const { w, h } = size;
    if (!w || !h || bands.length === 0 || periods.length < 2) return null;
    const mL = 14;
    const mR = 96; // 右侧留白放末列概念名
    const mT = 10;
    const mB = 26;
    const n = periods.length;
    const span = Math.max(1, w - mL - mR - TREND_NODE_W);
    const xs = periods.map((_, i) => mL + (span * i) / (n - 1));
    const H = h - mT - mB;

    // 每列出现的条带（值>0）：当期数量多的在上，排名随时间变化产生交叉
    const colCells: number[][] = periods.map((_, i) => {
      const idxs = bands.map((_, bi) => bi).filter((bi) => (bands[bi]!.values[i] ?? 0) > 0);
      idxs.sort((a, b) => bands[b]!.values[i]! - bands[a]!.values[i]! || a - b);
      return idxs;
    });

    // 全局比例尺：受最挤的列约束；单篇高度设上限，稀疏期不至于虚胖
    let unit = TREND_MAX_UNIT;
    for (let i = 0; i < n; i += 1) {
      const total = colCells[i]!.reduce((s, bi) => s + bands[bi]!.values[i]!, 0);
      const k = colCells[i]!.length;
      if (total > 0) unit = Math.min(unit, (H - TREND_GAP * (k - 1)) / total);
    }
    if (unit <= 0) return null;

    // 节点块：每列垂直居中堆叠
    const blocks: (TrendBlock | null)[][] = bands.map(() => periods.map(() => null));
    for (let i = 0; i < n; i += 1) {
      const idxs = colCells[i]!;
      const heights = idxs.map((bi) => Math.max(TREND_MIN_H, bands[bi]!.values[i]! * unit));
      const totalPx = heights.reduce((s, v) => s + v, 0) + TREND_GAP * (idxs.length - 1);
      let y = mT + Math.max(0, (H - totalPx) / 2);
      idxs.forEach((bi, j) => {
        blocks[bi]![i] = { x: xs[i]!, y0: y, y1: y + heights[j]! };
        y += heights[j]! + TREND_GAP;
      });
    }

    // 飘带：同一概念在相邻两列都出现才连；中断处自然断开
    const ribbons: { bi: number; d: string }[] = [];
    for (let bi = 0; bi < bands.length; bi += 1) {
      for (let i = 0; i < n - 1; i += 1) {
        const a = blocks[bi]![i];
        const b = blocks[bi]![i + 1];
        if (!a || !b) continue;
        const x1 = a.x + TREND_NODE_W;
        const x2 = b.x;
        const cx = (x2 - x1) * 0.45;
        ribbons.push({
          bi,
          d:
            `M ${x1.toFixed(1)} ${a.y0.toFixed(1)}` +
            ` C ${(x1 + cx).toFixed(1)} ${a.y0.toFixed(1)}, ${(x2 - cx).toFixed(1)} ${b.y0.toFixed(1)}, ${x2.toFixed(1)} ${b.y0.toFixed(1)}` +
            ` L ${x2.toFixed(1)} ${b.y1.toFixed(1)}` +
            ` C ${(x2 - cx).toFixed(1)} ${b.y1.toFixed(1)}, ${(x1 + cx).toFixed(1)} ${a.y1.toFixed(1)}, ${x1.toFixed(1)} ${a.y1.toFixed(1)}` +
            ' Z',
        });
      }
    }

    // 末列直接标注：自上而下排开、再从底部回推，避免重叠/越界
    const last = n - 1;
    const labels = colCells[last]!.map((bi) => {
      const blk = blocks[bi]![last]!;
      return { bi, y: (blk.y0 + blk.y1) / 2 };
    });
    labels.sort((a, b) => a.y - b.y);
    for (let i = 1; i < labels.length; i += 1) {
      labels[i]!.y = Math.max(labels[i]!.y, labels[i - 1]!.y + 12);
    }
    let bottom = h - mB - 4;
    for (let i = labels.length - 1; i >= 0; i -= 1) {
      labels[i]!.y = Math.min(labels[i]!.y, bottom);
      bottom = labels[i]!.y - 12;
    }
    const labelX = (xs[last] ?? 0) + TREND_NODE_W + 7;

    const step = Math.max(1, Math.ceil(n / 8));
    const ticks = periods
      .map((label, i) => ({ label, x: (xs[i] ?? 0) + TREND_NODE_W / 2 }))
      .filter((_, i) => i % step === 0 || i === n - 1);

    return { xs, blocks, ribbons, labels, labelX, ticks };
  }, [data, size]);

  const { bands, periods, unknown, rest } = data;
  const enough = bands.length >= 1 && periods.length >= 2;

  const handleMove = (band: TrendBand, ti?: number) => (e: React.MouseEvent) => {
    const svg = svgRef.current;
    if (!svg || !geom) return;
    const rect = svg.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    let col = ti ?? 0;
    if (ti === undefined) {
      for (let i = 1; i < geom.xs.length; i += 1) {
        if (Math.abs((geom.xs[i] ?? 0) - x) < Math.abs((geom.xs[col] ?? 0) - x)) col = i;
      }
    }
    setHover({ band, ti: col, x, y });
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
      <div className="row gap8 wrap" style={{ padding: '10px 16px 6px', flexShrink: 0 }}>
        <Segmented<TimelineGranularity>
          options={[
            { v: 'month', label: tr('按月份', 'By month') },
            { v: 'year', label: tr('按年份', 'By year') },
          ]}
          value={granularity}
          onChange={setGranularity}
        />
        <span className="mono muted" style={{ fontSize: 10.5, marginLeft: 'auto' }}>
          {tr('块高 = 当期关联论文数', 'Block height = papers linked in that period')}
          {rest > 0
            ? tr(` · 只画前 ${bands.length} 个概念（另 ${rest} 个未显示）`, ` · top ${bands.length} concepts shown (${rest} more hidden)`)
            : ''}
          {unknown > 0
            ? tr(` · ${unknown} 篇缺发表时间未计入`, ` · ${unknown} papers without a publish date excluded`)
            : ''}
        </span>
      </div>
      {enough && (
        <div className="row gap6 wrap" style={{ padding: '0 16px 8px', flexShrink: 0 }}>
          {bands.map((b) => (
            <span
              key={b.id}
              className="pill sm"
              title={tr('只看这个子主题', 'Focus on this subtopic')}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 5,
                background: 'var(--surface-2)',
                color: 'var(--text-2)',
                opacity: hover && hover.band.id !== b.id ? 0.45 : 1,
                cursor: 'pointer',
                transition: 'opacity 0.15s',
              }}
              onClick={() => onFocusConcept(b.id)}
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
            {tr(
              '编译并关联概念的论文还不够，暂时画不出趋势——至少需要覆盖两个时间点',
              'Not enough compiled papers with concept links to draw trends — at least two time points are needed',
            )}
          </div>
        ) : (
          geom && (
            <>
              <svg ref={svgRef} width={size.w} height={size.h} style={{ display: 'block' }} onMouseLeave={() => setHover(null)}>
                {/* 飘带（底层，半透明） */}
                {geom.ribbons.map(({ bi, d }, i) => {
                  const band = bands[bi]!;
                  return (
                    <path
                      key={`r-${band.id}-${i}`}
                      d={d}
                      fill={band.color}
                      fillOpacity={hover ? (hover.band.id === band.id ? 0.6 : 0.08) : 0.38}
                      style={{ cursor: 'pointer', transition: 'fill-opacity 0.15s' }}
                      onMouseMove={handleMove(band)}
                      onClick={() => onFocusConcept(band.id)}
                    />
                  );
                })}
                {/* 节点块（顶层，实色） */}
                {geom.blocks.map((row, bi) => {
                  const band = bands[bi]!;
                  return row.map(
                    (blk, i) =>
                      blk && (
                        <rect
                          key={`b-${band.id}-${periods[i]}`}
                          x={blk.x}
                          y={blk.y0}
                          width={TREND_NODE_W}
                          height={blk.y1 - blk.y0}
                          rx={1.5}
                          fill={band.color}
                          fillOpacity={hover ? (hover.band.id === band.id ? 1 : 0.25) : 0.95}
                          style={{ cursor: 'pointer', transition: 'fill-opacity 0.15s' }}
                          onMouseMove={handleMove(band, i)}
                          onClick={() => onFocusConcept(band.id)}
                        />
                      ),
                  );
                })}
                {/* 末列直接标注：色块 + 文字（文字用文本色，不用序列色） */}
                {geom.labels.map(({ bi, y }) => {
                  const band = bands[bi]!;
                  const dim = hover && hover.band.id !== band.id;
                  return (
                    <g key={`l-${band.id}`} opacity={dim ? 0.35 : 1} style={{ transition: 'opacity 0.15s' }}>
                      <rect x={geom.labelX} y={y - 3.5} width={7} height={7} rx={1.5} fill={band.color} />
                      <text x={geom.labelX + 11} y={y + 3.5} fontSize={10} fill="var(--text-2)" fontFamily="var(--sans)">
                        {truncate(band.label, 10)}
                      </text>
                    </g>
                  );
                })}
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
                    {periods[hover.ti]} · {hover.band.values[hover.ti]} {tr('篇', 'papers')}
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
    return (
      <div className="empty" style={{ margin: 'auto' }}>
        {tr('当前筛选下没有可聚类的论文', 'No papers to cluster under the current filter')}
      </div>
    );
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
                  title={tr('打开概念详情', 'Open concept detail')}
                  onClick={() => onOpenConcept(concept.id)}
                >
                  {concept.label}
                  <span style={{ opacity: 0.6, marginLeft: 5, fontSize: '0.85em' }}>{tr(meta.zh, meta.en)}</span>
                </span>
                <span className="mono muted" style={{ fontSize: 10.5 }}>{papers.length} {tr('篇', 'papers')}</span>
                <button className="btn btn-ghost sm" style={{ height: 22, fontSize: 10.5 }} onClick={() => onFocusConcept(concept.id)}>
                  <Icon name="search" size={11} />
                  {tr('只看这个子主题', 'Focus on this subtopic')}
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
              <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-3)' }}>
                {tr('未归类', 'Uncategorized')}
              </span>
              <span className="mono muted" style={{ fontSize: 10.5 }}>
                {tr(
                  `${topics.uncovered.length} 篇（尚未编译，暂无概念关联）`,
                  `${topics.uncovered.length} papers (not compiled yet, no concept links)`,
                )}
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

  if (isLoading) return <div className="empty" style={{ margin: 'auto' }}>{tr('正在构建图谱…', 'Building the graph…')}</div>;
  if (isError || !data) {
    return (
      <div style={{ margin: 'auto' }}>
        <EmptyState
          compact
          icon="x"
          title={tr('无法加载图谱', 'Failed to load graph')}
          desc={tr('后端不可用或接口尚未就绪，稍后重试。', 'Backend unavailable or API not ready — try again later.')}
        />
      </div>
    );
  }
  if (data.nodes.length === 0) {
    return (
      <div style={{ margin: 'auto' }}>
        <EmptyState
          compact
          icon="layers"
          title={tr('还没有可展示的网络', 'No network to show yet')}
          desc={tr(
            '先运行初始建库让论文通过筛选并完成编译，图谱会自动把论文、作者、概念连成网络。',
            'Run the initial library build so papers get screened and compiled — the graph then links papers, authors, and concepts automatically.',
          )}
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
            { v: 'network', label: tr('网络', 'Network') },
            { v: 'timeline', label: tr('时间线', 'Timeline') },
            { v: 'trends', label: tr('趋势', 'Trends') },
            { v: 'topics', label: tr('主题', 'Topics') },
          ]}
          value={view}
          onChange={setView}
        />
        <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)', marginLeft: 6 }}>{tr('子主题', 'Subtopic')}</span>
        <select
          className="input"
          style={{ height: 28, fontSize: 12, maxWidth: 220, padding: '0 8px' }}
          value={focusConceptId}
          onChange={(e) => setFocusConceptId(e.target.value)}
          title={tr('选一个概念，只看它牵出的论文子图', 'Pick a concept to see only the papers it links to')}
        >
          <option value="">{tr('全部主题', 'All topics')}</option>
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
          {data.truncated ? tr('论文较多，已按相关度展示前一批 · ', 'Large library — showing the top batch by relevance · ') : ''}
          {tr(
            `${model?.papers.length ?? 0} 篇论文 · ${model?.concepts.length ?? 0} 个概念`,
            `${model?.papers.length ?? 0} papers · ${model?.concepts.length ?? 0} concepts`,
          )}
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
