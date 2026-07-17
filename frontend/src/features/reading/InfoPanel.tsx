import { useCallback, useState } from 'react';
import { Icon } from '../../components/ui/Icon';
import { PaperStatusPill } from '../../components/ui/StatusPill';
import { RelevanceBar } from '../../components/ui/RelevanceBar';
import { EmptyState } from '../../components/ui/EmptyState';
import { FigureEmbed, FiguresSection, hasEmbeddedFigures, usePaperFigures } from '../../components/ui/FigureGallery';
import { Markdown, type WikiLinkHandler } from '../../lib/markdown';
import { fmtTime } from '../../lib/format';
import type { PaperDetail } from '../../lib/api';

/* ============================================================
   阅读工作台 · 论文信息面板（PaperDetailPane 的精简版）：
   元数据卡 + 摘要折叠 + 概念/标签 chips + wiki 正文 markdown。
   ============================================================ */

function MetaItem({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="row" style={{ gap: 10, padding: '3px 0', alignItems: 'flex-start' }}>
      <span className="mono" style={{ fontSize: 10.5, color: 'var(--accent-text)', width: 78, flexShrink: 0 }}>
        {label}
      </span>
      <span style={{ fontSize: 12, color: 'var(--text-2)', flex: 1, minWidth: 0, overflowWrap: 'break-word' }}>
        {children}
      </span>
    </div>
  );
}

export function InfoPanel({
  paper,
  onWikiLink,
}: {
  paper: PaperDetail;
  /** [[概念]] 双链与概念 chips 点击 → 按名称跳 wiki 概念库 */
  onWikiLink: WikiLinkHandler;
}) {
  const [abstractOpen, setAbstractOpen] = useState(false);
  const authors = paper.authors.map((a) => a.name).join(' · ');
  const arxivUrl = paper.arxiv_id ? `https://arxiv.org/abs/${paper.arxiv_id}` : null;
  const extUrl = arxivUrl ?? paper.url;

  // 正文 ![[fig:N]] 嵌入图（docs/api-lit.md §6.6）
  const figures = usePaperFigures(paper);
  const paperId = paper.id;
  const renderFigure = useCallback(
    (n: number) => {
      const fig = figures.find((f) => f.index === n);
      return fig ? <FigureEmbed paperId={paperId} fig={fig} /> : null;
    },
    [figures, paperId],
  );

  return (
    <div className="scroll" style={{ flex: 1, overflowY: 'auto', padding: '14px 16px 40px' }}>
      {/* —— 头部 —— */}
      <div className="row gap8 wrap" style={{ marginBottom: 8 }}>
        <PaperStatusPill status={paper.status} sm />
        {paper.venue && (
          <span className="pill sm" style={{ background: 'var(--surface-3)' }}>
            {paper.venue}
          </span>
        )}
        {typeof paper.note_count === 'number' && paper.note_count > 0 && (
          <span className="pill sm" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
            <Icon name="pen" size={10} />
            {paper.note_count} 条笔记
          </span>
        )}
      </div>
      <div style={{ fontSize: 14.5, fontWeight: 660, lineHeight: 1.4, marginBottom: 5 }}>{paper.title}</div>
      {authors && <div style={{ fontSize: 11.5, color: 'var(--text-3)', lineHeight: 1.5 }}>{authors}</div>}
      {extUrl && (
        <a
          className="btn btn-ghost sm"
          href={extUrl}
          target="_blank"
          rel="noreferrer noopener"
          style={{ textDecoration: 'none', marginTop: 10, display: 'inline-flex' }}
        >
          <Icon name="link" size={12} />
          {arxivUrl ? 'arXiv 原文' : '原文链接'}
        </a>
      )}

      {/* —— 元数据卡 —— */}
      <div className="card" style={{ margin: '14px 0 0', background: 'var(--surface-2)', padding: '10px 14px' }}>
        <MetaItem label="arxiv_id">
          {paper.arxiv_id ? <span className="mono">{paper.arxiv_id}</span> : <span className="muted">—</span>}
        </MetaItem>
        <MetaItem label="doi">
          {paper.doi ? <span className="mono">{paper.doi}</span> : <span className="muted">—</span>}
        </MetaItem>
        <MetaItem label="published">
          {paper.published_at ? <span className="mono">{paper.published_at.slice(0, 10)}</span> : <span className="muted">—</span>}
        </MetaItem>
        <MetaItem label="relevance">
          {paper.relevance_score !== null ? (
            <RelevanceBar value={paper.relevance_score} width={120} />
          ) : (
            <span className="muted">未打分</span>
          )}
        </MetaItem>
        <MetaItem label="ingested">
          <span className="mono">{fmtTime(paper.created_at)}</span>
        </MetaItem>
      </div>

      {/* —— 标签 —— */}
      {(paper.tags?.length ?? 0) > 0 && (
        <div className="row gap6 wrap" style={{ marginTop: 12 }}>
          {paper.tags!.map((t) => (
            <span key={t} className="tag">
              {t}
            </span>
          ))}
        </div>
      )}

      {/* —— 概念 chips —— */}
      {paper.concepts.length > 0 && (
        <div className="row gap6 wrap" style={{ marginTop: 12 }}>
          {paper.concepts.map((c) => (
            <span key={c.id} className="wikilink" style={{ height: 22 }} onClick={() => onWikiLink(c.name)}>
              {c.name}
            </span>
          ))}
        </div>
      )}

      {/* —— 摘要（折叠） —— */}
      {paper.abstract && (
        <div className="card" style={{ marginTop: 14, overflow: 'hidden' }}>
          <div
            className="row"
            onClick={() => setAbstractOpen((o) => !o)}
            style={{ padding: '9px 13px', cursor: 'pointer', justifyContent: 'space-between', userSelect: 'none' }}
          >
            <span style={{ fontSize: 12, fontWeight: 650 }}>
              摘要 <span className="en-label" style={{ fontSize: 10.5 }}>Abstract</span>
            </span>
            <Icon
              name="chevDown"
              size={13}
              style={{
                color: 'var(--text-3)',
                transform: abstractOpen ? 'rotate(180deg)' : 'none',
                transition: 'transform .15s',
              }}
            />
          </div>
          {abstractOpen && (
            <div style={{ padding: '0 13px 12px', fontSize: 12, lineHeight: 1.7, color: 'var(--text-2)' }}>
              {paper.abstract}
            </div>
          )}
        </div>
      )}

      {/* —— TL;DR —— */}
      {paper.tldr && (
        <div
          style={{
            marginTop: 14,
            padding: '10px 13px',
            borderRadius: 10,
            background: 'var(--accent-soft)',
            fontSize: 12.5,
            lineHeight: 1.65,
          }}
        >
          <span className="mono" style={{ fontSize: 10, color: 'var(--accent-text)', display: 'block', marginBottom: 3 }}>
            TL;DR
          </span>
          {paper.tldr}
        </div>
      )}

      {/* —— 重要图片画廊（正文已嵌图时默认折叠，避免重复视觉） —— */}
      <FiguresSection
        paper={paper}
        style={{ marginTop: 14 }}
        defaultCollapsed={hasEmbeddedFigures(paper.wiki_content, figures)}
      />

      {/* —— Wiki 正文（含 ![[fig:N]] 嵌入图） —— */}
      <div style={{ marginTop: 18 }}>
        {paper.wiki_content ? (
          <Markdown
            source={paper.wiki_content}
            onWikiLink={onWikiLink}
            renderFigure={renderFigure}
            style={{ fontSize: 12.5 }}
          />
        ) : (
          <EmptyState
            compact
            icon="pen"
            title="还没有 AI 精读页"
            desc="这篇论文还没被 AI 精读整理过（相关度不足，或还没运行初始建库 / 增量同步）。"
          />
        )}
      </div>
    </div>
  );
}
