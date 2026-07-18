import { useCallback, useEffect, useState } from 'react';
import type { CSSProperties } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { api, type ExperimentFigureInfo } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   实验图表画廊（docs/api-m5-a.md §3）：
   figures 步骤由 AI 写绘图脚本产出 PNG，
   数据源 GET /experiments/{id}/figures/{index}/image（blob）。
   复用论文 FigureGallery 的 blob 缓存 + Lightbox 模式：
   缩略图横向网格 + 点击开大图（左右切换 / Esc / 点击关闭）。
   ============================================================ */

function captionOf(fig: ExperimentFigureInfo): string {
  return fig.caption?.trim() || fig.name?.trim() || tr(`图 ${fig.index + 1}`, `Figure ${fig.index + 1}`);
}

/** 单张实验图 blob → objectURL；卸载 / 换图时 revoke。 */
function useExpFigureUrl(expId: string, index: number) {
  const query = useQuery({
    queryKey: ['experiment-figure-image', expId, index],
    queryFn: () => api.fetchExperimentFigureImage(expId, index),
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

const THUMB_W = 190;
const THUMB_H = 126;

function ExpFigureThumb({
  expId,
  fig,
  onClick,
}: {
  expId: string;
  fig: ExperimentFigureInfo;
  onClick: () => void;
}) {
  const { url, isLoading, isError } = useExpFigureUrl(expId, fig.index);
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
            <Icon name="chart" size={16} style={{ margin: '0 auto 4px' }} />
            <div style={{ fontSize: 10 }}>{tr('图片加载失败', 'Image failed to load')}</div>
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
        {captionOf(fig)}
      </div>
    </div>
  );
}

/* ---------------- Lightbox（全屏大图） ---------------- */

function ExpLightbox({
  expId,
  fig,
  index,
  count,
  onClose,
  onNav,
}: {
  expId: string;
  fig: ExperimentFigureInfo;
  index: number;
  count: number;
  onClose: () => void;
  onNav: (delta: number) => void;
}) {
  const { url, isLoading, isError } = useExpFigureUrl(expId, fig.index);

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
            <Icon name="chart" size={26} style={{ margin: '0 auto 8px' }} />
            <div style={{ fontSize: 13 }}>{tr('这张图加载失败了，稍后再试', 'This figure failed to load — try again later')}</div>
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
          {captionOf(fig)}
        </div>
        <div className="mono" style={{ marginTop: 6, fontSize: 11, color: 'var(--on-scrim-3)' }}>
          {index + 1} / {count}
          {fig.name ? ` · ${fig.name}` : ''}
        </div>
      </div>
    </div>
  );
}

/* ---------------- 画廊主体 ---------------- */

export function ExperimentFigures({ expId, figures }: { expId: string; figures: ExperimentFigureInfo[] }) {
  const [lightbox, setLightbox] = useState<number | null>(null);

  const nav = useCallback(
    (delta: number) => {
      setLightbox((i) => (i === null ? null : Math.min(figures.length - 1, Math.max(0, i + delta))));
    },
    [figures.length],
  );

  const lightboxFig = lightbox !== null ? figures[lightbox] : undefined;

  if (figures.length === 0) return null;

  return (
    <div>
      <div className="scroll" style={{ display: 'flex', gap: 12, overflowX: 'auto', paddingBottom: 4 }}>
        {figures.map((fig, i) => (
          <ExpFigureThumb key={fig.index} expId={expId} fig={fig} onClick={() => setLightbox(i)} />
        ))}
      </div>
      {lightbox !== null && lightboxFig && (
        <ExpLightbox
          expId={expId}
          fig={lightboxFig}
          index={lightbox}
          count={figures.length}
          onClose={() => setLightbox(null)}
          onNav={nav}
        />
      )}
    </div>
  );
}
