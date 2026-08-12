import {
  type CSSProperties,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { createPortal } from 'react-dom';
import { Document, Page, pdfjs } from 'react-pdf';
import 'react-pdf/dist/Page/TextLayer.css';
import 'react-pdf/dist/Page/AnnotationLayer.css';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import { Segmented } from '../../components/ui/Segmented';
import { toast } from '../../components/ui/Toast';
import { tr } from '../../lib/i18n';
import { apiBase } from '../../lib/endpoint';
import {
  api,
  ApiError,
  getToken,
  type HighlightColor,
  type HighlightCreateInput,
  type HighlightRead,
  type HighlightRect,
  type HighlightStyle,
  type PaperDetail,
} from '../../lib/api';
import { HIGHLIGHT_COLORS, HIGHLIGHT_STYLES, highlightColorMeta } from './shared';
import { PdfUploadButton } from '../shared/PdfUploadButton';

/* ============================================================
   自建 PDF 阅读器（pdf.js / react-pdf）：
   - 连续滚动渲染全部页 + 文本层（可选中）；
   - 划词后弹出配色条，一键生成划线；
   - 划线以归一化坐标存储，缩放/换宽度自动跟随；
   - 支持从右侧标注列表跳转并高亮闪烁。
   ============================================================ */

// pdf.js worker：Vite 用 import.meta.url 解析 node_modules 里的 worker 文件
pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  'pdfjs-dist/build/pdf.worker.min.mjs',
  import.meta.url,
).toString();

// 模块级常量，避免每次 render 生成新对象触发 react-pdf 重新加载。
// cMap / 标准字体数据必须提供，否则 pdf.js 画不出字形、整页空白（资源由 vite
// copyPdfAssets 插件从 pdfjs-dist 拷到 public/pdfjs 下，见 vite.config.ts）。
// 只取当前要渲染的那几段，不在后台把整份 PDF 拉完。25MB 的论文走校外代理约
// 0.5MB/s，整包下载要等 45 秒才出第一页；按需取段后首屏是几百 KB。
// 两个开关必须成对：disableAutoFetch 只是不主动补取缺失分段，disableStream 若不关，
// pdf.js 仍会顺着整份文件流式读到底——实测同样渲染 3 页，关掉后传输量 8.70MB → 3.76MB。
// 服务端不支持 Range 时 pdf.js 自动退回整包下载，行为与改造前一致。
const PDF_OPTIONS = {
  cMapUrl: '/pdfjs/cmaps/',
  cMapPacked: true,
  standardFontDataUrl: '/pdfjs/standard_fonts/',
  disableAutoFetch: true,
  disableStream: true,
  rangeChunkSize: 262144,
};

export interface JumpTarget {
  id: string;
  page: number;
  nonce: number;
}

/** 阅读器模式：annotate = 自建可划线阅读器；standard = 浏览器内置 PDF 阅读器（不支持划线）。 */
type ReaderMode = 'annotate' | 'standard';

// 缩放范围与步进（相对「适应宽度」的倍率）。
const ZOOM_MIN = 0.5;
const ZOOM_MAX = 3;
const ZOOM_STEP = 0.1;

interface PdfReaderProps {
  paper: PaperDetail;
  highlights: HighlightRead[];
  activeHighlightId: string | null;
  creating: boolean;
  onCreateHighlight: (input: HighlightCreateInput) => void;
  onHighlightClick: (id: string) => void;
  jumpTarget: JumpTarget | null;
}

/** 待确认的选区（等用户点配色条）。coords 为视口坐标，给浮动条定位。 */
interface Pending {
  page: number;
  rects: HighlightRect[];
  text: string;
  x: number;
  y: number; // 选区末行底部（视口坐标）——空间够时浮条放其下方
  yTop: number; // 选区末行顶部——空间不够时翻到其上方
}

const clamp01 = (n: number) => Math.min(1, Math.max(0, n));

/**
 * 只从选区内「真正有文字的文本节点」收集矩形。
 * pdf.js 文本层里图片/公式/大段空白区域没有文本节点，跨图选择时 range.getClientRects()
 * 会把选区覆盖的空白也画成一个巨型矩形——这是标注框过大的根因。逐个文本节点取矩形、
 * 首尾节点按选区偏移裁剪，空白区没有节点就自然不产生矩形。
 */
function collectTextRects(range: Range): DOMRect[] {
  const cac = range.commonAncestorContainer;
  // 选区落在单个文本节点内：直接用它的矩形
  if (cac.nodeType === Node.TEXT_NODE) {
    return Array.from(range.getClientRects()).filter((r) => r.width > 1 && r.height > 1);
  }
  const out: DOMRect[] = [];
  const walker = document.createTreeWalker(cac, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (!range.intersectsNode(node) || !(node.textContent ?? '').trim()) continue;
    const sub = document.createRange();
    sub.selectNodeContents(node);
    if (node === range.startContainer) sub.setStart(node, range.startOffset);
    if (node === range.endContainer) sub.setEnd(node, range.endOffset);
    for (const r of Array.from(sub.getClientRects())) {
      if (r.width > 1 && r.height > 1) out.push(r);
    }
  }
  return out;
}

// 标注渲染几何：高亮块压到行盒约 3/4 并略偏下（贴文字），下划线/波浪线贴文字底部。
const HL_TOP = 0.2; // 高亮块从行盒顶部裁掉的比例（去掉字上方行距）
const HL_HEIGHT = 0.68; // 高亮块占行盒高度的比例（≈ 文字高度的 3/4）
const UNDERLINE_TOP = 0.9; // 下划线相对行盒的纵向位置
const WAVE_TOP = 0.74; // 波浪线相对行盒的纵向位置（贴文字底部，波形上下居中于绘制带）
const WAVE_H = 8; // 波浪线绘制带高度（px）：波形上下都留出余量，下缘不再被裁

/**
 * 波浪线背景：可平铺的 SVG（高度与 WAVE_H 一致）。波形在 8×8 瓦片内上下居中——
 * 波峰约 y=2.5、波谷约 y=5.5，加描边仍落在 0..8 内，因此波谷（下缘）不会被裁掉。
 */
function waveBg(color: string): string {
  const svg = `<svg xmlns='http://www.w3.org/2000/svg' width='8' height='${WAVE_H}'><path d='M0 4 Q2 1 4 4 T8 4' fill='none' stroke='${color}' stroke-width='1.4'/></svg>`;
  return `url("data:image/svg+xml,${encodeURIComponent(svg)}")`;
}

/** 单个矩形按样式生成绝对定位样式；active 时只在标注下方加一条 border（不整框描边）。 */
function annotationRectStyle(
  r: HighlightRect,
  color: { solid: string; wash: string },
  style: HighlightStyle,
  active: boolean,
): CSSProperties {
  const rh = r.y1 - r.y0;
  const base: CSSProperties = {
    position: 'absolute',
    left: `${r.x0 * 100}%`,
    width: `${(r.x1 - r.x0) * 100}%`,
    cursor: 'pointer',
    pointerEvents: 'auto',
  };
  if (style === 'highlight') {
    return {
      ...base,
      top: `${(r.y0 + rh * HL_TOP) * 100}%`,
      height: `${rh * HL_HEIGHT * 100}%`,
      background: color.wash,
      mixBlendMode: 'multiply',
      borderRadius: 1.5,
      borderBottom: active ? `2px solid ${color.solid}` : undefined,
    };
  }
  // underline / wave：贴文字底部的一条线
  const isWave = style === 'wave';
  return {
    ...base,
    top: `${(r.y0 + rh * (isWave ? WAVE_TOP : UNDERLINE_TOP)) * 100}%`,
    height: isWave ? WAVE_H : active ? 3 : 2,
    background: isWave ? undefined : color.solid,
    backgroundImage: isWave ? waveBg(color.solid) : undefined,
    backgroundRepeat: isWave ? 'repeat-x' : undefined,
    backgroundSize: isWave ? `8px ${WAVE_H}px` : undefined,
    backgroundPosition: isWave ? 'center' : undefined,
    borderBottom: !isWave && active ? `1px solid ${color.solid}` : undefined,
  };
}

export function PdfReader({
  paper,
  highlights,
  activeHighlightId,
  creating,
  onCreateHighlight,
  onHighlightClick,
  jumpTarget,
}: PdfReaderProps) {
  const queryClient = useQueryClient();
  // 用回调 ref 而不是裸 useRef：滚动容器不是一挂载就存在的（论文原本没有 PDF、
  // 点「获取 PDF」后 pdf_available 才翻 true，容器这时才出现）。effect 只依赖 mode
  // 的话不会重跑，pageWidth 停在 0，一页都不渲染——表现就是整片深色背景。
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [scrollEl, setScrollEl] = useState<HTMLDivElement | null>(null);
  const attachScroll = useCallback((el: HTMLDivElement | null) => {
    scrollRef.current = el;
    setScrollEl(el);
  }, []);
  const pageWrapRefs = useRef<Map<number, HTMLDivElement>>(new Map());
  const [numPages, setNumPages] = useState(0);
  const [pageWidth, setPageWidth] = useState(0);
  const [scale, setScale] = useState(1); // 缩放倍率（1 = 适应宽度）
  const [mode, setMode] = useState<ReaderMode>('annotate'); // 标注阅读器 / 标准浏览器
  const [url, setUrl] = useState<string | null>(null);
  const [loadPct, setLoadPct] = useState<number | null>(null); // 整包下载时的进度，null = 还没有进度事件
  // 只渲染视口附近的页：每挂一个 <Page> 就要取那一页的数据，全挂等于把整份 PDF 下完，
  // 分段加载就白做了。未渲染的页留一个等高占位块，滚动条长度和跳转位置都不受影响。
  const [visiblePages, setVisiblePages] = useState<Set<number>>(() => new Set([1]));
  const pageHeights = useRef<Map<number, number>>(new Map()); // 渲染过的实际高度，占位块照它来
  const [pending, setPending] = useState<Pending | null>(null);
  const [pendingStyle, setPendingStyle] = useState<HighlightStyle>('highlight');
  const toolbarRef = useRef<HTMLDivElement>(null);
  const wheelAccum = useRef(0); // 捏合/滚轮缩放的手势累积量：攒够一档才缩放，避免频繁重渲染卡顿

  // 浮条渲染后按实际位置纠偏：若溢出窗口上/下缘，就把它推回可视区（不依赖坐标系假设）
  useLayoutEffect(() => {
    const el = toolbarRef.current;
    if (!el || !pending) return;
    const m = 8;
    const r = el.getBoundingClientRect();
    let dy = 0;
    if (r.bottom > window.innerHeight - m) dy = window.innerHeight - m - r.bottom;
    else if (r.top < m) dy = m - r.top;
    if (dy !== 0) el.style.top = `${parseFloat(el.style.top || '0') + dy}px`;
  }, [pending]);

  // 标注阅读器把地址直接交给 pdf.js，由它按需发 Range 请求；鉴权头随请求带上，
  // 所以这里不能用 blob。对象要 memo：react-pdf 认引用，每次新对象都会重新加载整份。
  const pdfSource = useMemo(() => {
    const src: { url: string; httpHeaders?: Record<string, string> } = {
      url: `${apiBase()}/papers/${paper.id}/pdf`,
    };
    const token = getToken();
    if (token) src.httpHeaders = { Authorization: `Bearer ${token}` };
    return src;
  }, [paper.id]);

  // 标准阅读器是 <iframe>，带不了 Authorization 头，只能整包下成 blob——所以推迟到
  // 真的切过去才下，默认的标注模式不再为它付这 25MB 的等待。
  const pdfQuery = useQuery({
    queryKey: ['paper-pdf', paper.id],
    queryFn: () => api.fetchPaperPdf(paper.id),
    enabled: mode === 'standard' && paper.pdf_available,
    retry: false,
    staleTime: Infinity,
  });

  // blob → objectURL（换论文/卸载时 revoke）
  useEffect(() => {
    const blob = pdfQuery.data;
    if (!blob) {
      setUrl(null);
      return;
    }
    const typed =
      blob.type === 'application/pdf' ? blob : new Blob([blob], { type: 'application/pdf' });
    const u = URL.createObjectURL(typed);
    setUrl(u);
    return () => URL.revokeObjectURL(u);
  }, [pdfQuery.data]);

  // 容器宽度 → 页宽（划线归一化存储，宽度变化自动跟随，无需重存）
  useEffect(() => {
    const el = scrollEl;
    if (!el) return;
    const measure = () => setPageWidth(Math.max(280, el.clientWidth - 28));
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [scrollEl, mode]); // 容器每次挂载都要重新测量并观察

  // 触控板双指捏合 / Ctrl(⌘)+滚轮 缩放：浏览器默认会整页缩放，这里拦下来只缩放 PDF。
  // 捏合手势在浏览器里表现为 ctrlKey=true 的 wheel 事件；必须用 passive:false 才能 preventDefault。
  // 每个 wheel 事件都改 scale 会让 react-pdf 每秒重渲染整页画布几十次而卡顿——这里改成
  // 离散步进：攒够一定手势量才跳一档（±ZOOM_STEP），缩放变「一档一档」但不再卡。
  useEffect(() => {
    const el = scrollEl;
    if (!el || mode !== 'annotate') return;
    const WHEEL_THRESHOLD = 45; // 累积 deltaY 达到此值才跳一档
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey && !e.metaKey) return; // 普通滚动不拦截，照常翻页
      e.preventDefault();
      wheelAccum.current += e.deltaY;
      if (Math.abs(wheelAccum.current) < WHEEL_THRESHOLD) return;
      const dir = wheelAccum.current > 0 ? -1 : 1; // 上滑放大、下滑缩小
      wheelAccum.current = 0;
      setScale((s) =>
        Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round((s + dir * ZOOM_STEP) * 100) / 100)),
      );
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, [scrollEl, mode]);

  // 换论文：渲染集合与页高缓存都要清掉，否则新文档沿用旧页高、占位块高度全错
  useEffect(() => {
    setVisiblePages(new Set([1]));
    pageHeights.current.clear();
    setNumPages(0);
  }, [paper.id]);

  // 视口观察：页容器进出可视区（上下各留一屏余量）时增减渲染集合
  const pageObserver = useRef<IntersectionObserver | null>(null);
  useEffect(() => {
    const root = scrollEl;
    if (!root || mode !== 'annotate') return;
    const io = new IntersectionObserver(
      (entries) => {
        setVisiblePages((prev) => {
          const next = new Set(prev);
          let changed = false;
          for (const e of entries) {
            const n = Number((e.target as HTMLElement).dataset.page);
            if (!n) continue;
            if (e.isIntersecting && !next.has(n)) {
              next.add(n);
              changed = true;
            } else if (!e.isIntersecting && next.has(n) && n !== 1) {
              next.delete(n);
              changed = true;
            }
          }
          return changed ? next : prev;
        });
      },
      { root, rootMargin: '150% 0px' },
    );
    pageObserver.current = io;
    for (const el of pageWrapRefs.current.values()) io.observe(el);
    return () => {
      io.disconnect();
      pageObserver.current = null;
    };
  }, [scrollEl, mode, numPages]);

  const fetchPdfMutation = useMutation({
    mutationFn: () => api.requestPaperPdf(paper.id),
    onSuccess: () => {
      toast('PDF 已下载好，正在打开', 'ok');
      void queryClient.invalidateQueries({ queryKey: ['paper-pdf', paper.id] });
      void queryClient.invalidateQueries({ queryKey: ['paper', paper.id] });
    },
    onError: (e) => {
      const msg =
        e instanceof ApiError && e.message.includes('PDF_FETCH_FAILED')
          ? '下载失败，源站暂时取不到，稍后再试'
          : e instanceof Error
            ? e.message
            : String(e);
      toast(`获取 PDF 失败：${msg}`, 'error');
    },
  });

  // —— 划词：mouseup 后读取选区，落到某一页并归一化 ——
  const captureSelection = useCallback(() => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) {
      setPending(null);
      return;
    }
    const text = sel.toString().trim();
    if (!text) {
      setPending(null);
      return;
    }
    const range = sel.getRangeAt(0);
    // 只取有文字的文本节点矩形：跨图/跨段的空白区没有文本节点，覆盖空白的巨型矩形不会进来
    const raw = collectTextRects(range);
    if (raw.length === 0) {
      setPending(null);
      return;
    }
    // 二次保险：极端情况下仍以中位行高滤掉异常超高矩形
    const sortedH = raw.map((r) => r.height).sort((a, b) => a - b);
    const medianH = sortedH[Math.floor(sortedH.length / 2)] ?? 0;
    const clientRects = medianH > 0 ? raw.filter((r) => r.height <= medianH * 2.2) : raw;
    if (clientRects.length === 0) {
      setPending(null);
      return;
    }
    // 每个 client rect 归到所属页，取命中最多的那一页（MVP：单页划线）
    const byPage = new Map<number, { wrap: DOMRect; rects: HighlightRect[] }>();
    for (const r of clientRects) {
      const cx = (r.left + r.right) / 2;
      const cy = (r.top + r.bottom) / 2;
      for (const [pageNo, wrapEl] of pageWrapRefs.current) {
        const wr = wrapEl.getBoundingClientRect();
        if (cx >= wr.left && cx <= wr.right && cy >= wr.top && cy <= wr.bottom) {
          const bucket = byPage.get(pageNo) ?? { wrap: wr, rects: [] };
          bucket.rects.push({
            x0: clamp01((r.left - wr.left) / wr.width),
            y0: clamp01((r.top - wr.top) / wr.height),
            x1: clamp01((r.right - wr.left) / wr.width),
            y1: clamp01((r.bottom - wr.top) / wr.height),
          });
          byPage.set(pageNo, bucket);
          break;
        }
      }
    }
    if (byPage.size === 0) {
      setPending(null);
      return;
    }
    let best: { page: number; rects: HighlightRect[] } | null = null;
    for (const [pageNo, bucket] of byPage) {
      if (!best || bucket.rects.length > best.rects.length) {
        best = { page: pageNo, rects: bucket.rects };
      }
    }
    const last = clientRects[clientRects.length - 1]!;
    setPending({
      page: best!.page,
      rects: best!.rects,
      text,
      x: last.left,
      y: last.bottom,
      yTop: last.top,
    });
  }, []);

  const onMouseUp = useCallback(() => {
    // 让浏览器先把选区结算好
    window.setTimeout(captureSelection, 0);
  }, [captureSelection]);

  const confirmHighlight = useCallback(
    (color: HighlightColor) => {
      if (!pending) return;
      onCreateHighlight({
        page: pending.page,
        rects: pending.rects,
        selected_text: pending.text,
        color,
        style: pendingStyle,
      });
      window.getSelection()?.removeAllRanges();
      setPending(null);
    },
    [pending, pendingStyle, onCreateHighlight],
  );

  // —— 跳转：定位到目标页并滚动 ——
  useEffect(() => {
    if (!jumpTarget) return;
    const el = pageWrapRefs.current.get(jumpTarget.page);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }, [jumpTarget]);

  // 按页分组的划线
  const highlightsByPage = useMemo(() => {
    const m = new Map<number, HighlightRead[]>();
    for (const h of highlights) {
      const arr = m.get(h.page) ?? [];
      arr.push(h);
      m.set(h.page, arr);
    }
    return m;
  }, [highlights]);

  // 缩放后的实际渲染宽度（划线归一化存储，随宽度自动缩放，无需重算坐标）。
  const renderWidth = Math.round(pageWidth * scale);
  const zoomBy = useCallback(
    (d: number) => setScale((s) => Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round((s + d) * 100) / 100))),
    [],
  );

  // 无 PDF：引导获取（原先靠「先整包下一遍，404 就是没有」判断，现在直接读元数据，
  // 免掉一次无谓的整包下载）
  if (!paper.pdf_available) {
    const canFetch = !!paper.arxiv_id;
    return (
      <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <EmptyState
          icon="file"
          title="该论文还没有 PDF"
          desc={
            canFetch
              ? undefined
              : '这篇论文不是 arXiv 来源，暂时不支持自动下载 PDF，可以通过右上角原文链接查看。'
          }
          action={(
            <div className="row gap8 wrap" style={{ justifyContent: 'center' }}>
              {canFetch ? (
                <button
                  className="btn btn-primary"
                  disabled={fetchPdfMutation.isPending}
                  onClick={() => fetchPdfMutation.mutate()}
                >
                  {fetchPdfMutation.isPending ? (
                    <>
                      <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
                      {tr('正在下载…', 'Downloading…')}
                    </>
                  ) : (
                    <>
                      <Icon name="download" size={14} />
                      {tr('获取 PDF', 'Fetch PDF')}
                    </>
                  )}
                </button>
              ) : paper.url ? (
                <a
                  className="btn btn-ghost"
                  href={paper.url}
                  target="_blank"
                  rel="noreferrer noopener"
                  style={{ textDecoration: 'none' }}
                >
                  <Icon name="link" size={14} />
                  {tr('打开原文链接', 'Open source link')}
                </a>
              ) : null}
              <PdfUploadButton paperId={paper.id} pdfAvailable={paper.pdf_available} />
            </div>
          )}
        />
      </div>
    );
  }

  return (
    <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      {/* —— 顶部控制条：阅读器模式切换 + 缩放 —— */}
      <div
        className="row"
        style={{ flexShrink: 0, gap: 10, padding: '8px 12px', borderBottom: '0.5px solid var(--border)' }}
      >
        <Segmented<ReaderMode>
          options={[
            { v: 'annotate', label: tr('标注阅读器', 'Annotate') },
            { v: 'standard', label: tr('标准阅读器', 'Standard') },
          ]}
          value={mode}
          onChange={setMode}
        />
        {mode === 'annotate' ? (
          <span className="row gap6" style={{ marginLeft: 'auto' }}>
            <button
              className="icon-btn"
              title={tr('缩小', 'Zoom out')}
              disabled={scale <= ZOOM_MIN}
              onClick={() => zoomBy(-ZOOM_STEP)}
              style={{ width: 26, height: 26 }}
            >
              <Icon name="minus" size={14} />
            </button>
            <button
              className="btn btn-ghost sm"
              title={tr('适应宽度', 'Fit width')}
              onClick={() => setScale(1)}
              style={{ minWidth: 52, justifyContent: 'center', fontVariantNumeric: 'tabular-nums' }}
            >
              {Math.round(scale * 100)}%
            </button>
            <button
              className="icon-btn"
              title={tr('放大', 'Zoom in')}
              disabled={scale >= ZOOM_MAX}
              onClick={() => zoomBy(ZOOM_STEP)}
              style={{ width: 26, height: 26 }}
            >
              <Icon name="plus" size={14} />
            </button>
          </span>
        ) : (
          <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-4)' }}>
            {tr('标准阅读器不支持划线标注', 'Standard viewer has no highlighting')}
          </span>
        )}
      </div>

      {mode === 'standard' ? (
        // 浏览器内置 PDF 阅读器：自带缩放/搜索/打印，但不承载我们的标注层。
        // 它只认能直接取到的地址，带不了鉴权头，所以这里必须等整包下完拿到 blob。
        url ? (
          <iframe
            title={paper.title}
            src={url}
            style={{ flex: 1, minHeight: 0, width: '100%', border: 'none', background: '#525659' }}
          />
        ) : (
          <div
            style={{
              flex: 1,
              minHeight: 0,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              background: '#525659',
              color: pdfQuery.isError ? '#fca5a5' : '#cbd5e1',
              fontSize: 12.5,
            }}
          >
            {pdfQuery.isError
              ? tr('PDF 加载失败，可以换回标注阅读器', 'Failed to load — try the annotating reader')
              : tr('正在下载完整 PDF…', 'Downloading the full PDF…')}
          </div>
        )
      ) : (
    <div
      ref={attachScroll}
      className="scroll"
      onMouseUp={onMouseUp}
      onMouseDown={() => setPending(null)}
      style={{ flex: 1, minHeight: 0, overflowY: 'auto', background: '#525659', padding: '14px 0' }}
    >
      <Document
        file={pdfSource}
        options={PDF_OPTIONS}
        onLoadSuccess={({ numPages: n }) => setNumPages(n)}
        onLoadProgress={({ loaded, total }) =>
          setLoadPct(total > 0 ? Math.min(99, Math.floor((loaded / total) * 100)) : null)
        }
        loading={
          // 网络慢时整份 PDF 可能要几分钟，不报进度的话跟卡死没有区别
          <div className="muted" style={{ textAlign: 'center', padding: 40, color: '#cbd5e1' }}>
            {loadPct == null
              ? tr('正在解析 PDF…', 'Loading the PDF…')
              : tr(`正在加载 PDF… ${loadPct}%`, `Loading the PDF… ${loadPct}%`)}
          </div>
        }
        error={
          // 任何加载失败都会走到这里（文件损坏、网络中断、被拦截），别把话说死成「损坏」
          <div className="muted" style={{ textAlign: 'center', padding: 40, color: '#fca5a5' }}>
            {tr('PDF 加载失败，请稍后重试。', 'Could not load this PDF — try again later.')}
          </div>
        }
      >
        {pageWidth > 0 &&
          Array.from({ length: numPages }, (_, i) => {
            const pageNo = i + 1;
            const pageHls = highlightsByPage.get(pageNo) ?? [];
            return (
              <div
                key={pageNo}
                data-page={pageNo}
                ref={(el) => {
                  if (el) {
                    pageWrapRefs.current.set(pageNo, el);
                    pageObserver.current?.observe(el);
                  } else pageWrapRefs.current.delete(pageNo);
                }}
                style={{ position: 'relative', width: renderWidth, margin: '0 auto 14px', boxShadow: '0 2px 10px rgba(0,0,0,0.4)' }}
              >
                {visiblePages.has(pageNo) ? (
                  <Page
                    pageNumber={pageNo}
                    width={renderWidth}
                    renderTextLayer
                    renderAnnotationLayer={false}
                    onRenderSuccess={({ height }) => pageHeights.current.set(pageNo, height)}
                    loading={
                      <div
                        className="pulse"
                        style={{ width: renderWidth, height: pageHeights.current.get(pageNo) ?? renderWidth * 1.29, background: 'var(--surface-3)' }}
                      />
                    }
                  />
                ) : (
                  <div
                    style={{ width: renderWidth, height: pageHeights.current.get(pageNo) ?? renderWidth * 1.29, background: 'var(--surface-3)' }}
                  />
                )}
                {/* 标注覆盖层：容器不吃事件，标注单独可点。按样式渲染高亮块/下划线/波浪线 */}
                <div style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
                  {pageHls.map((h) => {
                    const meta = highlightColorMeta(h.color);
                    const active = h.id === activeHighlightId;
                    return h.rects.map((r, ri) => (
                      <div
                        key={`${h.id}-${ri}`}
                        title={h.note ? `批注：${h.note}` : h.selected_text}
                        onClick={(e) => {
                          e.stopPropagation();
                          onHighlightClick(h.id);
                        }}
                        style={annotationRectStyle(r, meta, h.style, active)}
                      />
                    ));
                  })}
                </div>
              </div>
            );
          })}
      </Document>

      {/* —— 划词浮动配色条：portal 到 body，避免祖先 transform 破坏 fixed 定位 —— */}
      {pending &&
        createPortal(
          <div
            ref={toolbarRef}
            onMouseDown={(e) => e.stopPropagation()}
            style={{
              position: 'fixed',
            // 外层 Math.max 兜底：视口窄于浮条（280px）时上式会算出负值，把浮条推出左边界
            left: Math.max(8, Math.min(Math.max(pending.x, 10), window.innerWidth - 280)),
            // 下方空间不够（浮条约 40px 高）时翻到选区上方，避免超出窗口底部
            top:
              pending.y + 48 > window.innerHeight
                ? Math.max(8, pending.yTop - 48)
                : pending.y + 8,
            zIndex: 60,
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            padding: '7px 10px',
            borderRadius: 10,
            background: 'var(--surface)',
            border: '0.5px solid var(--border)',
            boxShadow: '0 6px 20px rgba(0,0,0,0.22)',
          }}
        >
          {/* 样式选择：高亮 / 下划线 / 波浪线 */}
          <span
            className="row"
            style={{ gap: 3, paddingRight: 5, marginRight: 1, borderRight: '1px solid var(--border-2)' }}
          >
            {HIGHLIGHT_STYLES.map((st) => (
              <button
                key={st.v}
                title={st.label}
                onClick={() => setPendingStyle(st.v)}
                style={{
                  width: 24,
                  height: 22,
                  borderRadius: 6,
                  padding: 0,
                  cursor: 'pointer',
                  background: pendingStyle === st.v ? 'var(--accent-soft)' : 'transparent',
                  border:
                    pendingStyle === st.v ? '1px solid var(--accent)' : '1px solid var(--border-2)',
                  display: 'flex',
                  alignItems: 'flex-end',
                  justifyContent: 'center',
                }}
              >
                <span
                  style={
                    st.v === 'highlight'
                      ? { width: 14, height: 9, background: 'rgba(245,197,24,0.5)', borderRadius: 1, marginBottom: 4 }
                      : st.v === 'underline'
                        ? { width: 14, borderBottom: '2px solid var(--text-3)', marginBottom: 5 }
                        : {
                            width: 14,
                            height: WAVE_H,
                            marginBottom: 2,
                            backgroundImage: waveBg('#888'),
                            backgroundRepeat: 'repeat-x',
                            backgroundSize: `8px ${WAVE_H}px`,
                            backgroundPosition: 'center',
                          }
                  }
                />
              </button>
            ))}
          </span>
          {HIGHLIGHT_COLORS.map((c) => (
            <button
              key={c.v}
              title={`${c.label}色`}
              disabled={creating}
              onClick={() => confirmHighlight(c.v)}
              style={{
                width: 20,
                height: 20,
                borderRadius: '50%',
                background: c.solid,
                border: '1.5px solid var(--surface)',
                boxShadow: '0 0 0 1px var(--border-2)',
                cursor: creating ? 'default' : 'pointer',
                padding: 0,
              }}
            />
          ))}
          </div>,
          document.body,
        )}
    </div>
      )}
    </div>
  );
}
