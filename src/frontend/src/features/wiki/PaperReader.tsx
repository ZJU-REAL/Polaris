import { useEffect, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { Icon } from '../../components/ui/Icon';
import { Markdown, type WikiLinkHandler } from '../../lib/markdown';
import type { PaperDetail } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   论文 wiki 阅览模式：把 AI 编译的图文介绍铺成一页干净、居中、
   适合专注阅读的长文；「导出 PDF」走浏览器打印（另存为 PDF），
   打印样式只保留正文（见 global.css @media print）。
   ============================================================ */

export function PaperReader({
  paper,
  renderFigure,
  onWikiLink,
  onFilterAuthor,
  onClose,
  autoPrint,
}: {
  paper: PaperDetail;
  renderFigure: (n: number) => ReactNode;
  onWikiLink?: WikiLinkHandler;
  /** 点击作者名 → 论文库按该作者过滤 */
  onFilterAuthor?: (name: string) => void;
  onClose: () => void;
  /** 打开后自动唤起打印对话框（「导出 PDF」一步直达） */
  autoPrint?: boolean;
}) {
  // 打开即打印：等一帧让正文（含图）渲染完再唤起打印
  useEffect(() => {
    if (!autoPrint) return;
    const t = setTimeout(() => window.print(), 350);
    return () => clearTimeout(t);
  }, [autoPrint]);

  // Esc 关闭 + 锁定背景滚动
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    // 仅在阅览模式打开时，打印样式才隐藏应用主体（见 global.css @media print）
    document.body.classList.add('reader-open');
    return () => {
      document.removeEventListener('keydown', onKey);
      document.body.style.overflow = prev;
      document.body.classList.remove('reader-open');
    };
  }, [onClose]);

  const venueYear = [paper.venue, paper.year].filter(Boolean).join(' · ');
  const readLink = `${window.location.origin}/papers/${paper.id}/read`;
  const sourceUrl = paper.arxiv_id ? `https://arxiv.org/abs/${paper.arxiv_id}` : paper.url ?? null;
  const sourceLabel = paper.arxiv_id ? `arXiv:${paper.arxiv_id}` : tr('论文源链接', 'Source');

  return createPortal(
    <div className="paper-reader">
      <div className="paper-reader-topbar">
        <button className="btn btn-ghost sm" onClick={onClose}>
          <Icon name="chevron" size={13} style={{ transform: 'rotate(180deg)' }} />
          {tr('退出阅览', 'Exit reading')}
        </button>
        <span className="paper-reader-topbar-title">{paper.title}</span>
        <button className="btn btn-primary sm" onClick={() => window.print()}>
          <Icon name="download" size={13} />
          {tr('导出 PDF', 'Export PDF')}
        </button>
      </div>

      <div className="paper-reader-scroll scroll">
        <article className="paper-reader-article">
          <header className="paper-reader-header">
            {venueYear && <div className="paper-reader-eyebrow mono">{venueYear}</div>}
            <h1>{paper.title}</h1>
            {paper.authors.length > 0 && (
              <div className="paper-reader-authors">
                {paper.authors.map((a, i) => (
                  <span key={`${a.name}-${i}`}>
                    {i > 0 && <span style={{ color: 'var(--text-4)' }}> · </span>}
                    {onFilterAuthor ? (
                      <span
                        className="author-link"
                        role="button"
                        tabIndex={0}
                        title={tr(`只看 ${a.name} 的论文`, `Show only ${a.name}'s papers`)}
                        onClick={() => onFilterAuthor(a.name)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault();
                            onFilterAuthor(a.name);
                          }
                        }}
                      >
                        {a.name}
                      </span>
                    ) : (
                      a.name
                    )}
                  </span>
                ))}
              </div>
            )}
            <div className="paper-reader-links">
              <a href={readLink} target="_blank" rel="noreferrer noopener">
                <Icon name="book" size={12} />
                {tr('平台阅读链接', 'Read on platform')}
              </a>
              {sourceUrl && (
                <a href={sourceUrl} target="_blank" rel="noreferrer noopener">
                  <Icon name="link" size={12} />
                  {sourceLabel}
                </a>
              )}
            </div>
          </header>

          {paper.tldr && (
            <div className="paper-reader-tldr">
              <span className="mono">TL;DR</span>
              <p>{paper.tldr}</p>
            </div>
          )}

          {paper.wiki_content ? (
            <div className="paper-reader-body">
              <Markdown source={paper.wiki_content} onWikiLink={onWikiLink} renderFigure={renderFigure} />
            </div>
          ) : (
            <p className="muted" style={{ fontSize: 13 }}>
              {tr('这篇论文还没有编译出图文介绍。', 'No compiled intro for this paper yet.')}
            </p>
          )}
        </article>
      </div>
    </div>,
    document.body,
  );
}
