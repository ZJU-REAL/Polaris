import { useCallback, useEffect, useState } from 'react';
import type { CSSProperties } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from './Icon';
import { toast } from './Toast';
import { api, ApiError, type FigureInfo, type PaperDetail } from '../../lib/api';

/* ============================================================
   论文图片画廊（docs/api-lit.md §6.5）：
   - FigureGallery：缩略图横向网格（默认只看重要图，可切换全部）
     + 点击开 Lightbox（大图 + 说明 + 左右切换 + Esc/点击关闭）
   - FiguresSection：wiki 详情 / 阅读页信息面板共用的小节封装
     （有图显示画廊；没图但有 PDF 时显示「提取图片」按钮）
   ============================================================ */

function captionOf(fig: FigureInfo): string {
  return fig.caption?.trim() || `第 ${fig.page} 页的图`;
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
            <div style={{ fontSize: 10 }}>图片加载失败</div>
          </div>
        ) : (
          <img
            src={url}
            alt={captionOf(fig)}
            loading="lazy"
            style={{ width: '100%', height: '100%', objectFit: 'contain', background: '#fff' }}
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
    background: 'rgba(255,255,255,0.14)',
    color: '#fff',
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
        background: 'rgba(10, 22, 44, 0.82)',
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
        aria-label="关闭"
        onClick={onClose}
        style={{
          position: 'absolute',
          top: 16,
          right: 18,
          width: 34,
          height: 34,
          borderRadius: 17,
          border: 'none',
          background: 'rgba(255,255,255,0.14)',
          color: '#fff',
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
            aria-label="上一张"
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
            aria-label="下一张"
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
            style={{ width: 420, maxWidth: '80vw', height: 300, borderRadius: 10, background: 'rgba(255,255,255,0.14)' }}
          />
        ) : isError || !url ? (
          <div style={{ textAlign: 'center', color: 'rgba(255,255,255,0.75)' }}>
            <Icon name="file" size={26} style={{ margin: '0 auto 8px' }} />
            <div style={{ fontSize: 13 }}>这张图加载失败了，稍后再试</div>
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
              background: '#fff',
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
            color: 'rgba(255,255,255,0.88)',
          }}
        >
          {captionOf(fig)}
        </div>
        <div className="mono" style={{ marginTop: 6, fontSize: 11, color: 'rgba(255,255,255,0.55)' }}>
          {index + 1} / {count} · 第 {fig.page} 页
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
          {showAll ? '只看重要的' : `显示全部 ${figures.length} 张`}
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

export function FiguresSection({ paper, style }: { paper: PaperDetail; style?: CSSProperties }) {
  const queryClient = useQueryClient();

  // PaperDetail 未带 figures 字段时兜底拉一次列表（接口未就绪则静默降级）
  const figuresQuery = useQuery({
    queryKey: ['paper-figures', paper.id],
    queryFn: () => api.listFigures(paper.id),
    enabled: paper.figures === undefined,
    retry: false,
  });
  const figures = paper.figures ?? figuresQuery.data ?? [];

  const extractMutation = useMutation({
    mutationFn: () => api.extractFigures(paper.id),
    onSuccess: (res) => {
      queryClient.setQueryData<FigureInfo[]>(['paper-figures', paper.id], res.figures);
      queryClient.setQueryData<PaperDetail>(['paper', paper.id], (old) =>
        old ? { ...old, figures: res.figures } : old,
      );
      if (res.figures.length > 0) {
        toast(`提取完成，找到 ${res.figures.length} 张图`, 'ok');
      } else {
        toast('这篇 PDF 里没找到合适的图片', 'info');
      }
    },
    onError: (e) => {
      const msg =
        e instanceof ApiError && e.message.includes('PDF_NOT_AVAILABLE')
          ? '这篇论文还没有 PDF，先到阅读页获取 PDF 再试'
          : e instanceof Error
            ? e.message
            : String(e);
      toast(`提取图片失败：${msg}`, 'error');
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
          onClick={() => extractMutation.mutate()}
        >
          {extractMutation.isPending ? (
            <>
              <Icon name="refresh" size={13} style={{ animation: 'spin 1s linear infinite' }} />
              提取中，视觉模型筛选约需半分钟…
            </>
          ) : (
            <>
              <Icon name="sparkle" size={13} />
              提取图片
            </>
          )}
        </button>
      </div>
    );
  }

  return (
    <div style={{ marginTop: 18, ...style }}>
      <div style={{ fontSize: 12.5, fontWeight: 650, marginBottom: 8 }}>
        重要图片 <span className="en-label" style={{ fontSize: 11 }}>Figures</span>
      </div>
      <FigureGallery paperId={paper.id} figures={figures} />
    </div>
  );
}
