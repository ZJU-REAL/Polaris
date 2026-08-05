import { useEffect, useState } from 'react';
import { api } from '../../lib/api';
import { tr } from '../../lib/i18n';
import type { ImageRef } from '../../lib/assistantStream';

/* ============================================================
   工具返回的图片。

   对话流里传的是**出处**不是字节：一张图 base64 之后几百 KB，几张就能把 SSE 连接和
   浏览器一起灌爆。取图走平台本来就有的鉴权端点（`<img src>` 带不了 Bearer，所以是
   blob → objectURL，这是全仓已有的做法）。

   取不到就不占位：图注还在，用户知道有这么一张图、也知道它没显示出来——比一个碎图
   图标诚实。
   ============================================================ */

function FigureImage({ figure, onOpen }: { figure: ImageRef; onOpen: (url: string) => void }) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    let objectUrl: string | null = null;
    void api
      .fetchFigureImage(figure.paperId, figure.index)
      .then((blob) => {
        if (!alive) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => {
        if (alive) setFailed(true);
      });
    return () => {
      alive = false;
      // objectURL 不撤会一直占着内存，长对话里几十张图就很可观
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [figure.paperId, figure.index]);

  if (failed) {
    return (
      <div style={{ fontSize: 11, color: 'var(--text-4)', padding: '4px 0' }}>
        {tr(`图 ${figure.index}`, `Figure ${figure.index}`)}
        {figure.label ? ` · ${figure.label}` : ''}
        {tr('（取图失败）', ' (failed to load)')}
      </div>
    );
  }
  return (
    <figure style={{ margin: '6px 0' }}>
      {url ? (
        <img
          src={url}
          alt={figure.label ?? `figure ${figure.index}`}
          onClick={() => onOpen(url)}
          style={{
            maxWidth: '100%',
            borderRadius: 8,
            border: '0.5px solid var(--border-2)',
            cursor: 'zoom-in',
            display: 'block',
          }}
        />
      ) : (
        <div
          style={{
            height: 84,
            borderRadius: 8,
            background: 'var(--surface-2)',
            border: '0.5px solid var(--border-2)',
          }}
        />
      )}
      {figure.label && (
        <figcaption style={{ fontSize: 11, color: 'var(--text-4)', marginTop: 4, lineHeight: 1.5 }}>
          {tr(`图 ${figure.index}`, `Fig. ${figure.index}`)} · {figure.label}
        </figcaption>
      )}
    </figure>
  );
}

export function ToolImages({ images }: { images: ImageRef[] }) {
  // 点开看大图。灯箱只是放大同一个 objectURL，不再取一次
  const [zoomed, setZoomed] = useState<string | null>(null);
  return (
    <>
      {images.map((figure) => (
        <FigureImage
          key={`${figure.paperId}-${figure.index}`}
          figure={figure}
          onOpen={setZoomed}
        />
      ))}
      {zoomed && (
        <div
          onClick={() => setZoomed(null)}
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0,0,0,0.72)',
            zIndex: 90,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            padding: 24,
            cursor: 'zoom-out',
          }}
        >
          <img
            src={zoomed}
            alt=""
            style={{ maxWidth: '100%', maxHeight: '100%', borderRadius: 8 }}
          />
        </div>
      )}
    </>
  );
}
