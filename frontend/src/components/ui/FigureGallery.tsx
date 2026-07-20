import { useCallback, useEffect, useState } from 'react';
import type { CSSProperties } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from './Icon';
import { toast } from './Toast';
import { tr } from '../../lib/i18n';
import { api, ApiError, type FigureInfo, type PaperDetail } from '../../lib/api';

/* ============================================================
   论文图片画廊（docs/api-lit.md §6.5）：
   - FigureGallery：缩略图横向网格（默认只看重要图，可切换全部）
     + 点击开 Lightbox（大图 + 说明 + 左右切换 + Esc/点击关闭）
   - FigureEmbed：wiki 正文 ![[fig:N]] 嵌入图（§6.6）：居中大图
     + 灰色图注，点击开 Lightbox
   - FiguresSection：wiki 详情 / 阅读页信息面板共用的小节封装
     （有图显示画廊；没图但有 PDF 时显示「提取图片」按钮；
     正文已嵌图时可 defaultCollapsed 折叠，避免重复视觉）
   ============================================================ */

function captionOf(fig: FigureInfo): string {
  return fig.caption?.trim() || tr(`第 ${fig.page} 页的图`, `Figure on page ${fig.page}`);
}

/** 图片类型标签；无类型返回 null（不显示）。 */
const FIGURE_KIND: Record<string, { zh: string; en: string }> = {
  motivation: { zh: '动机图', en: 'Motivation' },
  method: { zh: '方法图', en: 'Method' },
  architecture: { zh: '架构图', en: 'Architecture' },
  experiment: { zh: '实验图', en: 'Experiment' },
};

function kindLabelOf(fig: FigureInfo): string | null {
  const m = fig.kind ? FIGURE_KIND[fig.kind] : undefined;
  return m ? tr(m.zh, m.en) : null;
}

/** 图注前的小型类型标签（嵌入图/缩略图/大图共用）。 */
function KindTag({ fig, light }: { fig: FigureInfo; light?: boolean }) {
  const label = kindLabelOf(fig);
  if (!label) return null;
  return (
    <span
      className="mono"
      style={{
        display: 'inline-block',
        marginRight: 6,
        padding: '0 5px',
        borderRadius: 4,
        fontSize: '0.85em',
        lineHeight: 1.7,
        background: light ? 'var(--scrim-btn)' : 'var(--accent-soft)',
        color: light ? 'var(--on-scrim)' : 'var(--accent-text)',
      }}
    >
      {label}
    </span>
  );
}

/** wiki 正文里是否有能解析到实际图片的 ![[fig:N]] 标记（用于详情页把画廊默认折叠）。 */
export function hasEmbeddedFigures(content: string | null | undefined, figures: FigureInfo[]): boolean {
  if (!content || figures.length === 0) return false;
  const re = /!\[\[fig:(\d+)\]\]/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(content))) {
    const idx = Number(m[1]);
    if (figures.some((f) => f.index === idx)) return true;
  }
  return false;
}

/** 单张图 blob → objectURL；卸载 / 换图时 revoke。 */
function useFigureUrl(paperId: string, index: number) {
  const query = useQuery({
    queryKey: ['figure-image', paperId, index],
    queryFn: () => api.fetchFigureImage(paperId, index),
    staleTime: Infinity,
    retry: false,
  });
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    const blob = query.data;
    if (!blob) {
      setUrl(null);
      return;
    }
    const u = URL.createObjectURL(blob);
    setUrl(u);
    return () => URL.revokeObjectURL(u);
  }, [query.data]);
  return { url, isLoading: query.isLoading, isError: query.isError };
}

/* ---------------- 正文嵌入图（![[fig:N]]） ---------------- */

/**
 * wiki 正文里的嵌入图：居中大图 + 下方灰色图注。
 * 点击开 Lightbox：传 onOpen 用外部 lightbox，否则用内置单图 Lightbox。
 */
export function FigureEmbed({
  paperId,
  fig,
  onOpen,
}: {
  paperId: string;
  fig: FigureInfo;
  onOpen?: () => void;
}) {
  const { url, isLoading, isError } = useFigureUrl(paperId, fig.index);
  const [lightboxOpen, setLightboxOpen] = useState(false);

  const open = () => {
    if (onOpen) onOpen();
    else setLightboxOpen(true);
  };

  return (
    <figure style={{ margin: 0, textAlign: 'center' }}>
      {isLoading ? (
        <div
          className="pulse"
          style={{
            width: 'min(420px, 100%)',
            height: 200,
            margin: '0 auto',
            borderRadius: 10,
            background: 'var(--surface-3)',
          }}
        />
      ) : isError || !url ? (
        <div
          style={{
            width: 'min(420px, 100%)',
            margin: '0 auto',
            padding: '26px 16px',
            borderRadius: 10,
            border: '0.5px dashed var(--border)',
            background: 'var(--surface-2)',
            color: 'var(--text-4)',
          }}
        >
          <Icon name="file" size={18} style={{ margin: '0 auto 6px' }} />
          <div style={{ fontSize: 11.5 }}>{tr('这张图加载失败了，稍后再试', 'This figure failed to load, try again later')}</div>
        </div>
      ) : (
        <img
          src={url}
          alt={captionOf(fig)}
          loading="lazy"
          onClick={open}
          style={{
            maxWidth: '100%',
            maxHeight: 420,
            borderRadius: 10,
            border: '0.5px solid var(--border)',
            background: 'var(--media-bg)',
            cursor: 'zoom-in',
            objectFit: 'contain',
          }}
        />
      )}
      <figcaption
        style={{
          marginTop: 7,
          fontSize: 11.5,
          lineHeight: 1.6,
          color: 'var(--text-3)',
          maxWidth: 560,
          marginLeft: 'auto',
          marginRight: 'auto',
        }}
      >
        <KindTag fig={fig} />
        {captionOf(fig)}
      </figcaption>
      {lightboxOpen && (
        <Lightbox
          paperId={paperId}
          fig={fig}
          index={0}
          count={1}
          onClose={() => setLightboxOpen(false)}
          onNav={() => {}}
        />
      )}
    </figure>
  );
}

/* ---------------- 缩略图 ---------------- */

const THUMB_W = 150;
const THUMB_H = 100;

function FigureThumb({
  paperId,
  fig,
  onClick,
}: {
  paperId: string;
  fig: FigureInfo;
  onClick: () => void;
}) {
  const { url, isLoading, isError } = useFigureUrl(paperId, fig.index);
  return (
    <div
      title={captionOf(fig)}
      onClick={url ? onClick : undefined}
      style={{ width: THUMB_W, flexShrink: 0, cursor: url ? 'zoom-in' : 'default' }}
    >
      <div
        style={{
          width: THUMB_W,
          height: THUMB_H,
          borderRadius: 8,
          border: '0.5px solid var(--border)',
          background: 'var(--surface)',
          overflow: 'hidden',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        {isLoading ? (
          <div className="pulse" style={{ width: '100%', height: '100%', background: 'var(--surface-3)' }} />
        ) : isError || !url ? (
          <div style={{ textAlign: 'center', color: 'var(--text-4)' }}>
            <Icon name="file" size={16} style={{ margin: '0 auto 4px' }} />
            <div style={{ fontSize: 10 }}>{tr('图片加载失败', 'Failed to load')}</div>
          </div>
        ) : (
          <img
            src={url}
            alt={captionOf(fig)}
            loading="lazy"
            style={{ width: '100%', height: '100%', objectFit: 'contain', background: 'var(--media-bg)' }}
          />
        )}
      </div>
      <div
        style={{
          marginTop: 5,
          fontSize: 10.5,
          lineHeight: 1.45,
          color: 'var(--text-3)',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
      >
        <KindTag fig={fig} />
        {captionOf(fig)}
      </div>
    </div>
  );
}

/* ---------------- Lightbox（全屏大图） ---------------- */

function Lightbox({
  paperId,
  fig,
  index,
  count,
  onClose,
  onNav,
}: {
  paperId: string;
  /** 当前展示的图 */
  fig: FigureInfo;
  /** 当前图在列表中的下标 */
  index: number;
  /** 列表总数 */
  count: number;
  onClose: () => void;
  /** 相对切换：-1 上一张 / +1 下一张 */
  onNav: (delta: number) => void;
}) {
  const { url, isLoading, isError } = useFigureUrl(paperId, fig.index);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
      else if (e.key === 'ArrowLeft') onNav(-1);
      else if (e.key === 'ArrowRight') onNav(1);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose, onNav]);

  const navBtn: CSSProperties = {
    position: 'absolute',
    top: '50%',
    transform: 'translateY(-50%)',
    width: 38,
    height: 38,
    borderRadius: 19,
    border: 'none',
    background: 'var(--scrim-btn)',
    color: 'var(--on-scrim)',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    cursor: 'pointer',
  };

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 70,
        background: 'var(--scrim)',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '48px 72px',
        animation: 'fadeUp 0.15s ease',
      }}
    >
      {/* 关闭 */}
      <button
        aria-label={tr('关闭', 'Close')}
        onClick={onClose}
        style={{
          position: 'absolute',
          top: 16,
          right: 18,
          width: 34,
          height: 34,
          borderRadius: 17,
          border: 'none',
          background: 'var(--scrim-btn)',
          color: 'var(--on-scrim)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          cursor: 'pointer',
        }}
      >
        <Icon name="x" size={16} />
      </button>

      {/* 上一张 / 下一张 */}
      {count > 1 && (
        <>
          <button
            aria-label={tr('上一张', 'Previous')}
            disabled={index === 0}
            style={{ ...navBtn, left: 18, opacity: index === 0 ? 0.3 : 1 }}
            onClick={(e) => {
              e.stopPropagation();
              onNav(-1);
            }}
          >
            <Icon name="chevron" size={17} style={{ transform: 'rotate(180deg)' }} />
          </button>
          <button
            aria-label={tr('下一张', 'Next')}
            disabled={index === count - 1}
            style={{ ...navBtn, right: 18, opacity: index === count - 1 ? 0.3 : 1 }}
            onClick={(e) => {
              e.stopPropagation();
              onNav(1);
            }}
          >
            <Icon name="chevron" size={17} />
          </button>
        </>
      )}

      {/* 大图 */}
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          maxWidth: '100%',
          maxHeight: '100%',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          minHeight: 0,
        }}
      >
        {isLoading ? (
          <div
            className="pulse"
            style={{ width: 420, maxWidth: '80vw', height: 300, borderRadius: 10, background: 'var(--scrim-btn)' }}
          />
        ) : isError || !url ? (
          <div style={{ textAlign: 'center', color: 'var(--on-scrim-2)' }}>
            <Icon name="file" size={26} style={{ margin: '0 auto 8px' }} />
            <div style={{ fontSize: 13 }}>{tr('这张图加载失败了，稍后再试', 'This figure failed to load, try again later')}</div>
          </div>
        ) : (
          <img
            key={fig.index}
            src={url}
            alt={captionOf(fig)}
            style={{
              maxWidth: '84vw',
              maxHeight: '74vh',
              objectFit: 'contain',
              borderRadius: 10,
              background: 'var(--media-bg)',
              boxShadow: 'var(--shadow-win)',
            }}
          />
        )}
        <div
          style={{
            marginTop: 14,
            maxWidth: 720,
            textAlign: 'center',
            fontSize: 12.5,
            lineHeight: 1.6,
            color: 'var(--on-scrim)',
          }}
        >
          <KindTag fig={fig} light />
          {captionOf(fig)}
        </div>
        <div className="mono" style={{ marginTop: 6, fontSize: 11, color: 'var(--on-scrim-3)' }}>
          {index + 1} / {count} · {tr(`第 ${fig.page} 页`, `page ${fig.page}`)}
        </div>
      </div>
    </div>
  );
}

/* ---------------- 画廊主体 ---------------- */

export function FigureGallery({ paperId, figures }: { paperId: string; figures: FigureInfo[] }) {
  const [showAll, setShowAll] = useState(false);
  const [lightbox, setLightbox] = useState<number | null>(null);

  const important = figures.filter((f) => f.important);
  const hasImportant = important.length > 0;
  const shown = showAll || !hasImportant ? figures : important;

  const nav = useCallback(
    (delta: number) => {
      setLightbox((i) => (i === null ? null : Math.min(shown.length - 1, Math.max(0, i + delta))));
    },
    [shown.length],
  );

  const lightboxFig = lightbox !== null ? shown[lightbox] : undefined;

  if (figures.length === 0) return null;

  return (
    <div>
      <div className="scroll" style={{ display: 'flex', gap: 10, overflowX: 'auto', paddingBottom: 4 }}>
        {shown.map((fig, i) => (
          <FigureThumb key={fig.index} paperId={paperId} fig={fig} onClick={() => setLightbox(i)} />
        ))}
      </div>
      {hasImportant && figures.length > important.length && (
        <button
          className="btn btn-ghost sm"
          style={{ marginTop: 6 }}
          onClick={() => {
            setLightbox(null);
            setShowAll((s) => !s);
          }}
        >
          <Icon name={showAll ? 'chevDown' : 'grid'} size={12} style={showAll ? { transform: 'rotate(180deg)' } : undefined} />
          {showAll ? tr('只看重要的', 'Key figures only') : tr(`显示全部 ${figures.length} 张`, `Show all ${figures.length}`)}
        </button>
      )}
      {lightbox !== null && lightboxFig && (
        <Lightbox
          paperId={paperId}
          fig={lightboxFig}
          index={lightbox}
          count={shown.length}
          onClose={() => setLightbox(null)}
          onNav={nav}
        />
      )}
    </div>
  );
}

/* ---------------- 小节封装（wiki 详情 / 阅读页共用） ---------------- */

/**
 * 论文图片列表：PaperDetail 自带 figures 优先，缺失时兜底拉一次列表
 * （接口未就绪则静默降级为 []）。paper 未加载完成时传 undefined，返回 []。
 */
export function usePaperFigures(paper: { id: string; figures?: FigureInfo[] } | undefined): FigureInfo[] {
  const figuresQuery = useQuery({
    queryKey: ['paper-figures', paper?.id],
    queryFn: () => api.listFigures(paper!.id),
    enabled: !!paper && paper.figures === undefined,
    retry: false,
  });
  return paper?.figures ?? figuresQuery.data ?? [];
}

export function FiguresSection({
  paper,
  style,
  defaultCollapsed = false,
}: {
  paper: PaperDetail;
  style?: CSSProperties;
  /** 正文已内嵌图片时默认折叠画廊，避免重复视觉 */
  defaultCollapsed?: boolean;
}) {
  const queryClient = useQueryClient();
  const figures = usePaperFigures(paper);

  const [expanded, setExpanded] = useState(!defaultCollapsed);
  // 换论文 / 折叠策略变化时重置展开状态
  useEffect(() => {
    setExpanded(!defaultCollapsed);
  }, [paper.id, defaultCollapsed]);

  const extractMutation = useMutation({
    mutationFn: (force: boolean) => api.extractFigures(paper.id, force),
    onSuccess: (res, force) => {
      queryClient.setQueryData<FigureInfo[]>(['paper-figures', paper.id], res.figures);
      queryClient.setQueryData<PaperDetail>(['paper', paper.id], (old) =>
        old ? { ...old, figures: res.figures } : old,
      );
      if (res.figures.length > 0) {
        toast(
          force
            ? tr(
                `重新提取完成，找到 ${res.figures.length} 张图；如需更新正文插图请点「重新编译」`,
                `Re-extracted ${res.figures.length} figures; use “Recompile” to refresh in-text figures`,
              )
            : tr(`提取完成，找到 ${res.figures.length} 张图`, `Done — found ${res.figures.length} figures`),
          'ok',
        );
      } else {
        toast(tr('这篇 PDF 里没找到合适的图片', 'No suitable figures found in this PDF'), 'info');
      }
    },
    onError: (e) => {
      const msg =
        e instanceof ApiError && e.message.includes('PDF_NOT_AVAILABLE')
          ? tr('这篇论文还没有 PDF，先到阅读页获取 PDF 再试', 'This paper has no PDF yet — fetch it on the reading page first')
          : e instanceof Error
            ? e.message
            : String(e);
      toast(tr(`提取图片失败：${msg}`, `Figure extraction failed: ${msg}`), 'error');
    },
  });

  // 没图：有 PDF 时给一个小按钮按需提取；没 PDF 就什么都不显示
  if (figures.length === 0) {
    if (!paper.pdf_available) return null;
    return (
      <div style={{ marginTop: 16, ...style }}>
        <button
          className="btn btn-ghost sm"
          disabled={extractMutation.isPending}
          onClick={() => extractMutation.mutate(false)}
        >
          {extractMutation.isPending ? (
            <>
              <Icon name="refresh" size={13} style={{ animation: 'spin 1s linear infinite' }} />
              {tr('提取中，视觉模型筛选约需半分钟…', 'Extracting — vision-model filtering takes about half a minute…')}
            </>
          ) : (
            <>
              <Icon name="sparkle" size={13} />
              {tr('提取图片', 'Extract figures')}
            </>
          )}
        </button>
      </div>
    );
  }

  return (
    <div style={{ marginTop: 18, ...style }}>
      <div className="row gap8" style={{ marginBottom: expanded ? 8 : 0 }}>
        <span style={{ fontSize: 12.5, fontWeight: 650 }}>
          {tr('重要图片', 'Figures')}
        </span>
        {defaultCollapsed && (
          <button className="btn btn-ghost sm" onClick={() => setExpanded((e) => !e)}>
            <Icon
              name="chevDown"
              size={12}
              style={{ transform: expanded ? 'rotate(180deg)' : 'none', transition: 'transform .15s' }}
            />
            {expanded ? tr('收起', 'Collapse') : tr(`展开全部图片（${figures.length} 张）`, `Show all figures (${figures.length})`)}
          </button>
        )}
        {paper.pdf_available && (
          <button
            className="btn btn-ghost sm"
            style={{ marginLeft: 'auto' }}
            disabled={extractMutation.isPending}
            title={tr('用最新方法从 PDF 重新抽取图片', 'Re-extract figures from the PDF with the latest method')}
            onClick={() => extractMutation.mutate(true)}
          >
            <Icon
              name="refresh"
              size={12}
              style={extractMutation.isPending ? { animation: 'spin 1s linear infinite' } : undefined}
            />
            {extractMutation.isPending ? tr('重新提取中…', 'Re-extracting…') : tr('重新提取配图', 'Re-extract')}
          </button>
        )}
      </div>
      {expanded && <FigureGallery paperId={paper.id} figures={figures} />}
    </div>
  );
}
