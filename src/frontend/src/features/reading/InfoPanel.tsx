import { useCallback, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { CompileBadge } from '../../components/ui/CompileBadge';
import { EmptyState } from '../../components/ui/EmptyState';
import { FigureEmbed, FiguresSection, hasEmbeddedFigures, usePaperFigures } from '../../components/ui/FigureGallery';
import { Markdown, type WikiLinkHandler } from '../../lib/markdown';
import { fmtTime } from '../../lib/format';
import { clickable } from '../../lib/a11y';
import { type PaperConceptRef, type PaperDetail } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { libraryPath } from '../libraries/hooks';
import { PaperMyTagsRow } from '../shared/PaperDetailBlocks';
import { PaperFact, PaperFacts, PaperKeyPoints, PaperKicker, splitSummary } from '../shared/PaperDetail';
import { PaperFulltextFact } from '../shared/PaperFulltext';
import { relevanceWords } from '../shared/PaperRow';
import { MetaFold } from '../wiki/shared';
import { evidenceCitationRenderer, evidenceRefs } from './evidenceArtifact';
import '../shared/papers.css';

/* ============================================================
   阅读页 · 「论文信息」：和文献库里的论文详情同一个顺序，只是更窄。

     出处 · 标题 · 作者
     事实行（为什么在这里 / PDF 与全文 / 我的标签）
     要点（只出现一次）
     解读正文（出处可点，跳到原文那一句）
     概念 · 摘要 · 编号与日期（折叠）
   ============================================================ */

function MetaItem({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="row" style={{ gap: 10, padding: '3px 0', alignItems: 'flex-start' }}>
      <span style={{ fontSize: 12, color: 'var(--text-3)', width: 78, flexShrink: 0 }}>{label}</span>
      <span style={{ fontSize: 12, color: 'var(--text-2)', flex: 1, minWidth: 0, overflowWrap: 'break-word' }}>{children}</span>
    </div>
  );
}

export function InfoPanel({
  paper,
  onWikiLink,
  onOpenConcept,
}: {
  paper: PaperDetail;
  /** 正文里的 [[概念]] 双链 → 按名称打开概念页 */
  onWikiLink: WikiLinkHandler;
  /** 概念 chip 自带 id，直接按 id 打开概念页 */
  onOpenConcept: (concept: PaperConceptRef) => void;
}) {
  const navigate = useNavigate();
  // 作者点了去这篇论文所在的文献库按作者过滤；不在任何文献库里时就只是文字
  const libHref = paper.library_id ? libraryPath(paper.library_id) : null;

  const figures = usePaperFigures(paper);
  const paperId = paper.id;
  const renderFigure = useCallback(
    (n: number) => {
      const fig = figures.find((f) => f.index === n);
      return fig ? <FigureEmbed paperId={paperId} fig={fig} /> : null;
    },
    [figures, paperId],
  );
  const refs = useMemo(() => evidenceRefs(paper.wiki_evidence), [paper.wiki_evidence]);
  const renderCitation = useMemo(
    () => evidenceCitationRenderer({ libraryId: paper.library_id, fallbackPaperId: paper.id, title: paper.title, refs }),
    [refs, paper.library_id, paper.id, paper.title],
  );
  const split = useMemo(() => splitSummary(paper.wiki_content), [paper.wiki_content]);
  const relevance = paper.relevance_score;

  return (
    <div className="scroll" style={{ flex: 1, overflowY: 'auto', padding: '14px 16px 40px' }} data-testid="reader-info">
      <PaperKicker year={paper.year} venue={paper.venue && !/^[a-z-]+(\.[A-Za-z-]{2,})?$/.test(paper.venue) ? paper.venue : null} arxivId={paper.arxiv_id} doi={paper.doi} url={paper.url} />
      <div style={{ fontSize: 'var(--fs-title)', fontWeight: 600, lineHeight: 1.4, margin: '6px 0 5px' }}>{paper.title}</div>
      {paper.authors.length > 0 && (
        <div style={{ fontSize: 12, color: 'var(--text-3)', lineHeight: 1.6 }}>
          {paper.authors.map((a, i) => {
            const affil = a.affiliations?.filter(Boolean) ?? [];
            return (
              <span key={`${a.name}-${i}`}>
                {i > 0 && ' · '}
                <span
                  className={libHref ? 'author-link' : undefined}
                  title={affil.length > 0 ? `${a.name} — ${affil.join('; ')}` : undefined}
                  {...(libHref ? clickable(() => navigate(`${libHref}?author=${encodeURIComponent(a.name)}`)) : {})}
                >
                  {a.name}
                </span>
              </span>
            );
          })}
        </div>
      )}

      <PaperFacts>
        {relevance !== null && relevance !== undefined && (
          <PaperFact label={tr('为什么在这里', 'Why it’s here')}>
            {tr(`${relevanceWords(relevance)}（${relevance.toFixed(2)}）`, `${relevanceWords(relevance)} (${relevance.toFixed(2)})`)}
          </PaperFact>
        )}
        <PaperFact label={tr('PDF 与全文', 'PDF & full text')}>
          <PaperFulltextFact libraryId={paper.library_id ?? undefined} paperId={paper.id} pdfAvailable={!!paper.pdf_available} doi={paper.doi} />
        </PaperFact>
        <PaperFact label={tr('我的标签', 'My tags')}>
          <PaperMyTagsRow bare paperId={paper.id} myTags={paper.my_tags} detailKey={['paper', paper.id]} />
        </PaperFact>
      </PaperFacts>

      {split.lead ? (
        <PaperKeyPoints>
          <Markdown source={split.lead} onWikiLink={onWikiLink} renderCitation={renderCitation} style={{ fontSize: 13 }} />
        </PaperKeyPoints>
      ) : (
        <PaperKeyPoints tldr={paper.tldr} />
      )}

      <div style={{ marginTop: 18 }} data-testid="reader-summary">
        {paper.wiki_content ? (
          <>
            <div className="pd-byline" style={{ marginTop: 0 }}>
              <span className="row gap8">
                {tr('AI 生成的解读', 'AI-generated summary')}
                <CompileBadge model={paper.compiled_model} at={paper.compiled_at} />
              </span>
            </div>
            <Markdown
              source={split.body || paper.wiki_content}
              onWikiLink={onWikiLink}
              renderFigure={renderFigure}
              renderCitation={renderCitation}
              style={{ fontSize: 13 }}
            />
          </>
        ) : (
          <EmptyState
            compact
            icon="pen"
            title={tr('还没有解读', 'No summary yet')}
            desc={tr('可以在文献库里为它生成解读。', 'You can generate one from its library.')}
          />
        )}
      </div>

      <FiguresSection paper={paper} style={{ marginTop: 14 }} defaultCollapsed={hasEmbeddedFigures(paper.wiki_content, figures)} />

      {paper.concepts.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <div className="pd-more-label">{tr('涉及的概念', 'Concepts')}</div>
          <div className="row gap6 wrap">
            {paper.concepts.map((c) => (
              <span key={c.id} className="wikilink" style={{ height: 22 }} {...clickable(() => onOpenConcept(c))}>
                {c.name}
              </span>
            ))}
          </div>
        </div>
      )}

      {paper.abstract && (
        <MetaFold label={tr('摘要', 'Abstract')} style={{ margin: '14px 0 0' }}>
          <div style={{ fontSize: 12.5, lineHeight: 1.7, color: 'var(--text-2)' }}>{paper.abstract}</div>
        </MetaFold>
      )}
      <MetaFold label={tr('编号与日期', 'Identifiers and dates')} style={{ margin: '10px 0 0' }}>
        <MetaItem label={tr('arXiv 编号', 'arXiv ID')}>
          {paper.arxiv_id ? <span className="mono">{paper.arxiv_id}</span> : <span className="muted">—</span>}
        </MetaItem>
        <MetaItem label="DOI">{paper.doi ? <span className="mono">{paper.doi}</span> : <span className="muted">—</span>}</MetaItem>
        <MetaItem label={tr('发表日期', 'Published')}>
          {paper.published_at ? <span className="mono">{paper.published_at.slice(0, 10)}</span> : <span className="muted">—</span>}
        </MetaItem>
        <MetaItem label={tr('加入时间', 'Added')}>
          <span className="mono">{fmtTime(paper.created_at)}</span>
        </MetaItem>
      </MetaFold>
    </div>
  );
}
