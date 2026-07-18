import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Document, Page, pdfjs } from 'react-pdf';
import 'react-pdf/dist/Page/TextLayer.css';
import 'react-pdf/dist/Page/AnnotationLayer.css';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import {
  api,
  ApiError,
  type HighlightColor,
  type HighlightCreateInput,
  type HighlightRead,
  type HighlightRect,
  type PaperDetail,
} from '../../lib/api';
import { HIGHLIGHT_COLORS, highlightColorMeta } from './shared';

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
const PDF_OPTIONS = {
  cMapUrl: '/pdfjs/cmaps/',
  cMapPacked: true,
  standardFontDataUrl: '/pdfjs/standard_fonts/',
};

export interface JumpTarget {
  id: string;
  page: number;
  nonce: number;
}

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
  y: number;
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
  const scrollRef = useRef<HTMLDivElement>(null);
  const pageWrapRefs = useRef<Map<number, HTMLDivElement>>(new Map());
  const [numPages, setNumPages] = useState(0);
  const [pageWidth, setPageWidth] = useState(0);
  const [url, setUrl] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);

  const pdfQuery = useQuery({
    queryKey: ['paper-pdf', paper.id],
    queryFn: () => api.fetchPaperPdf(paper.id),
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
    const el = scrollRef.current;
    if (!el) return;
    const measure = () => setPageWidth(Math.max(280, el.clientWidth - 28));
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [url]);

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
      });
      window.getSelection()?.removeAllRanges();
      setPending(null);
    },
    [pending, onCreateHighlight],
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

  if (pdfQuery.isLoading) {
    return (
      <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <div style={{ textAlign: 'center' }}>
          <div
            className="pulse"
            style={{ width: 220, height: 300, margin: '0 auto 16px', borderRadius: 10, background: 'var(--surface-3)' }}
          />
          <div className="muted" style={{ fontSize: 12.5 }}>正在加载 PDF…</div>
        </div>
      </div>
    );
  }

  // 无 PDF：引导获取（沿用原逻辑）
  if (pdfQuery.isError || !url) {
    const canFetch = !!paper.arxiv_id;
    return (
      <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <EmptyState
          icon="file"
          title="该论文还没有 PDF"
          desc={
            canFetch
              ? '可以从 arXiv 自动下载一份，下载后就能在这里阅读、划线。'
              : '这篇论文不是 arXiv 来源，暂时不支持自动下载 PDF，可以通过右上角原文链接查看。'
          }
          action={
            canFetch ? (
              <button
                className="btn btn-primary"
                disabled={fetchPdfMutation.isPending}
                onClick={() => fetchPdfMutation.mutate()}
              >
                {fetchPdfMutation.isPending ? (
                  <>
                    <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
                    正在下载…
                  </>
                ) : (
                  <>
                    <Icon name="download" size={14} />
                    获取 PDF
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
                打开原文链接
              </a>
            ) : undefined
          }
        />
      </div>
    );
  }

  return (
    <div
      ref={scrollRef}
      className="scroll"
      onMouseUp={onMouseUp}
      onMouseDown={() => setPending(null)}
      style={{ flex: 1, minHeight: 0, overflowY: 'auto', background: '#525659', padding: '14px 0' }}
    >
      <Document
        file={url}
        options={PDF_OPTIONS}
        onLoadSuccess={({ numPages: n }) => setNumPages(n)}
        loading={
          <div className="muted" style={{ textAlign: 'center', padding: 40, color: '#cbd5e1' }}>
            正在解析 PDF…
          </div>
        }
        error={
          <div className="muted" style={{ textAlign: 'center', padding: 40, color: '#fca5a5' }}>
            PDF 打不开，文件可能损坏。
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
                ref={(el) => {
                  if (el) pageWrapRefs.current.set(pageNo, el);
                  else pageWrapRefs.current.delete(pageNo);
                }}
                style={{ position: 'relative', width: pageWidth, margin: '0 auto 14px', boxShadow: '0 2px 10px rgba(0,0,0,0.4)' }}
              >
                <Page
                  pageNumber={pageNo}
                  width={pageWidth}
                  renderTextLayer
                  renderAnnotationLayer={false}
                  loading={
                    <div
                      className="pulse"
                      style={{ width: pageWidth, height: pageWidth * 1.29, background: 'var(--surface-3)' }}
                    />
                  }
                />
                {/* 划线覆盖层：容器不吃事件，色块单独可点 */}
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
                        style={{
                          position: 'absolute',
                          left: `${r.x0 * 100}%`,
                          top: `${r.y0 * 100}%`,
                          width: `${(r.x1 - r.x0) * 100}%`,
                          height: `${(r.y1 - r.y0) * 100}%`,
                          background: meta.wash,
                          mixBlendMode: 'multiply',
                          cursor: 'pointer',
                          pointerEvents: 'auto',
                          borderRadius: 1.5,
                          outline: active ? `2px solid ${meta.solid}` : 'none',
                          outlineOffset: 1,
                          transition: 'outline-color 0.15s',
                        }}
                      />
                    ));
                  })}
                </div>
              </div>
            );
          })}
      </Document>

      {/* —— 划词浮动配色条 —— */}
      {pending && (
        <div
          onMouseDown={(e) => e.stopPropagation()}
          style={{
            position: 'fixed',
            left: Math.min(Math.max(pending.x, 10), window.innerWidth - 210),
            top: Math.min(pending.y + 8, window.innerHeight - 52),
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
          <Icon name="pen" size={12} style={{ color: 'var(--text-4)' }} />
          {HIGHLIGHT_COLORS.map((c) => (
            <button
              key={c.v}
              title={`${c.label}色划线`}
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
        </div>
      )}
    </div>
  );
}
