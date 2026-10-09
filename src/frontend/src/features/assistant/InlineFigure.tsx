import { useEffect, useState } from 'react';
import { api } from '../../lib/api';
import { tr } from '../../lib/i18n';

/** 正文里 ![图注](paper_id/图号) 渲染出来的配图。

    与工具卡片里的图共用同一条路：`<img src>` 带不了 Bearer，所以 blob → objectURL。
    取不到就什么都不画——正文里插一个碎图比少一张图更打断阅读。 */
export function InlineFigure({ paperId, index }: { paperId: string; index: number }) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    let objectUrl: string | null = null;
    void api
      .fetchFigureImage(paperId, index)
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
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [paperId, index]);

  if (failed) return null;
  if (!url) {
    return (
      <span style={{ fontSize: 12, color: 'var(--text-3)' }}>{tr('图片加载中…', 'Loading figure…')}</span>
    );
  }
  return (
    <a href={`/papers/${paperId}/read`} title={tr('打开论文', 'Open paper')}>
      <img
        src={url}
        onError={() => setFailed(true)}
        alt={tr('论文图片', 'Paper figure')}
        style={{
          display: 'block',
          maxWidth: '100%',
          maxHeight: 280,
          margin: '8px 0',
          borderRadius: 8,
          border: '0.5px solid var(--border-2)',
        }}
      />
    </a>
  );
}
