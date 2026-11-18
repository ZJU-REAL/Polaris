import { useCallback, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { CompileBadge } from '../../components/ui/CompileBadge';
import {
  FigureEmbed,
  FiguresSection,
  hasEmbeddedFigures,
  usePaperFigures,
} from '../../components/ui/FigureGallery';
import { DotsMenu } from '../../components/ui/DotsMenu';
import { Markdown } from '../../lib/markdown';
import {
  api,
  type LibraryEntry,
  type PaperAuthor,
  type Publication,
  type ReadingStatus,
} from '../../lib/api';
import { tr } from '../../lib/i18n';
import { libraryPath, useTopicLibrary } from '../libraries/hooks';
import { readerFrom } from '../reading/shared';
import { PaperReader } from '../wiki/PaperReader';
import { PaperFact, PaperFacts, PaperKeyPoints, PaperKicker, splitSummary } from '../shared/PaperDetail';
import { PaperFulltextFact } from '../shared/PaperFulltext';
import { relevanceWords } from '../shared/PaperRow';
import '../shared/papers.css';
import { AuthorLinks, MetaFold, usePoolConceptNav } from '../wiki/shared';
import {
  ConceptChips,
  PaperMyMetaRow,
  PaperMyTagsRow,
  PaperNotesSection,
  WikiHeaderActions,
} from '../shared/PaperDetailBlocks';

/* ============================================================
   我的文献库 · 右栏详情（三个 tab 共用）：
   - 活体论文（paperId 非 null）→ 拉论文详情；有 wiki 用文献追踪
     同款 markdown 渲染（含 ![[fig:N]] 嵌入图与 [[概念]] 双链，
     双链跳转对齐阅读页：/libraries/<库>?concept=名称）；
   - 活体但没有 wiki → 元数据 + 摘要/TL;DR + 一句提示；
   - 快照条目（论文已删）→ 拉单条详情取 wiki 快照正文；有则渲染
     markdown（图片已随论文删除，![[fig:N]] 用灰字占位），
     无则退回快照元数据 + 摘要 + 外链按钮。
   ============================================================ */

/** 右栏展示用的条目快照：库条目 / 发表条目统一成这一个形状。 */
export interface DetailSnapshot {
  title: string;
  authors: PaperAuthor[];
  year: number | null;
  venue: string | null;
  arxivId: string | null;
  doi: string | null;
  url: string | null;
  abstract?: string | null;
  tldr?: string | null;
  citedByCount?: number;
}

export function entrySnapshot(e: LibraryEntry): DetailSnapshot {
  return {
    title: e.title,
    authors: e.authors,
    year: e.year,
    venue: e.venue,
    arxivId: e.arxiv_id,
    doi: e.doi,
    url: e.url,
    abstract: e.abstract,
    tldr: e.tldr,
  };
}

export function pubSnapshot(p: Publication): DetailSnapshot {
  return {
    title: p.title,
    authors: p.authors,
    year: p.year,
    venue: p.venue,
    arxivId: p.arxiv_id,
    doi: p.doi,
    url: p.url,
    citedByCount: p.cited_by_count,
  };
}

/** frontmatter 风格元数据行（同文献追踪详情面板版式）。 */
function MetaItem({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="row" style={{ gap: 12, padding: '4px 0', alignItems: 'flex-start' }}>
      <span className="mono" style={{ fontSize: 11, color: 'var(--accent-text)', width: 88, flexShrink: 0 }}>
        {label}
      </span>
      <span style={{ fontSize: 13, color: 'var(--text-2)', flex: 1, minWidth: 0, overflowWrap: 'break-word' }}>
        {children}
      </span>
    </div>
  );
}

export function LibraryDetailPane({
  paperId,
  snapshot,
  entryId,
  onFilterAuthor,
  onFilterAffiliation,
}: {
  /** 活体论文 id；null = 快照条目（论文已删，只展示快照元数据）。 */
  paperId: string | null;
  snapshot: DetailSnapshot;
  /** 收藏/浏览记录条目 id；提供后论文已删时可回退到条目的 wiki 快照（发表 tab 不传）。 */
  entryId?: string;
  /** 点作者 → 按该作者过滤列表；不传则作者名不可点 */
  onFilterAuthor?: (name: string) => void;
  /** 点机构 chip → 按该机构过滤列表；不传则 chips 不可点 */
  onFilterAffiliation?: (name: string) => void;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const [readerOpen, setReaderOpen] = useState(false);
  // 阅览模式打开后是否直接唤起打印（「导出 PDF」一步直达）
  const [readerPrint, setReaderPrint] = useState(false);
  const openReader = useCallback(() => {
    setReaderPrint(false);
    setReaderOpen(true);
  }, []);
  const openReaderForPrint = useCallback(() => {
    setReaderPrint(true);
    setReaderOpen(true);
  }, []);

  // 与文献追踪详情面板同一个 queryKey（['paper', id]），缓存互通
  const detailKey = useMemo(() => ['paper', paperId], [paperId]);
  const paperQuery = useQuery({
    queryKey: detailKey,
    queryFn: () => api.getPaper(paperId ?? ''),
    enabled: paperId !== null,
    retry: false,
  });
  const paper = paperId !== null && paperQuery.isSuccess ? paperQuery.data : undefined;

  // 星标 / 阅读状态、笔记改完刷新个人库列表（行上有星标与笔记数）
  const listKeys = useMemo(() => [['library']], []);
  const noteKeys = useMemo(() => [detailKey, ['library']], [detailKey]);

  // 论文已删（last_paper_id 为 null，或详情拉取失败）→ 拉条目详情取 wiki 快照
  const wantSnapshotWiki = entryId !== undefined && (paperId === null || paperQuery.isError);
  const entryQuery = useQuery({
    queryKey: ['library-entry', entryId],
    queryFn: () => api.getLibraryEntry(entryId ?? ''),
    enabled: wantSnapshotWiki,
    retry: false,
  });
  const snapshotWiki =
    wantSnapshotWiki && entryQuery.isSuccess ? entryQuery.data.wiki_content : null;

  // 正文 ![[fig:N]] 嵌入图（同文献追踪）
  const figures = usePaperFigures(paper);
  const renderFigure = useCallback(
    (n: number) => {
      const fig = figures.find((f) => f.index === n);
      return fig && paper ? <FigureEmbed paperId={paper.id} fig={fig} /> : null;
    },
    [figures, paper],
  );

  // 快照 wiki 里的 ![[fig:N]]：图片文件已随论文删除，渲染成灰字占位
  const renderSnapshotFigure = useCallback(
    () => (
      <span className="muted" style={{ fontSize: 12 }}>
        {tr('（图片已删除）', '(Figure deleted)')}
      </span>
    ),
    [],
  );

  // 「去文献库」按钮的落点：后端直接给的 library_id；旧后端没这个字段时按起源课题反查
  const topicLib = useTopicLibrary(paper?.library_id ? null : paper?.project_id ?? null);
  const paperLibId = paper?.library_id ?? topicLib?.id ?? null;

  // 我的文献库是池级上下文：概念一律进不限库的概念页
  const { openConcept, openConceptByName } = usePoolConceptNav();

  if (paperId !== null && paperQuery.isLoading) {
    return <div className="empty" style={{ margin: 'auto' }}>{tr('加载中…', 'Loading…')}</div>;
  }

  // 详情拉不到（比如论文刚被删）时退回快照展示
  const alive = paper !== undefined;
  const title = paper?.title ?? snapshot.title;
  const authors = paper?.authors ?? snapshot.authors;
  // 机构只有活体论文有（个人库条目快照不存机构）；论文被删的条目这一行自然消失
  const affiliations = paper?.affiliations;
  const year = paper?.year ?? snapshot.year;
  const venue = paper?.venue ?? snapshot.venue;
  const arxivId = paper?.arxiv_id ?? snapshot.arxivId;
  const doi = paper?.doi ?? snapshot.doi;
  const url = paper?.url ?? snapshot.url;
  const abstract = paper?.abstract ?? snapshot.abstract ?? null;
  const tldr = paper?.tldr ?? snapshot.tldr ?? null;
  const arxivUrl = arxivId ? `https://arxiv.org/abs/${arxivId}` : null;
  const readingStatus: ReadingStatus = paper?.reading_status ?? 'unread';

  return (
    <div
      className="scroll fadeup"
      key={paperId ?? snapshot.title}
      style={{ overflowY: 'auto', flex: 1, padding: '26px 32px 60px' }}
    >
      {/* 和文献库里的论文详情同一个顺序（features/shared/PaperDetail）：出处 · 标题 · 作者 → 一个主动作 →
          事实行 → 要点（只出现一次）→ 摘要 / 笔记 / 解读 */}
      <PaperKicker
        year={paper?.year ?? snapshot.year}
        venue={venue && !/^[a-z-]+(\.[A-Za-z-]{2,})?$/.test(venue) ? venue : null}
        arxivId={paper?.arxiv_id ?? snapshot.arxivId}
        doi={paper?.doi ?? snapshot.doi}
        url={url}
        extra={!alive ? tr('原文已删除，这里是保存时的副本', 'The paper was deleted; this is the copy you saved') : undefined}
      />
      <h1 className="pd-title">{title}</h1>
      <AuthorLinks authors={authors} onFilter={onFilterAuthor} />
      {affiliations && affiliations.length > 0 && (
        <div className="pd-affil">
          {affiliations.slice(0, 4).map((a, i) => (
            <span key={a}>
              {i > 0 && ' · '}
              <span className="author-link" role="button" tabIndex={0} onClick={() => onFilterAffiliation?.(a)} onKeyDown={(e) => { if (e.key === 'Enter') onFilterAffiliation?.(a); }}>
                {a}
              </span>
            </span>
          ))}
        </div>
      )}

      {alive && (
        <div className="pd-actions">
          <button
            className="btn btn-primary sm"
            onClick={() => navigate(`/papers/${paper.id}/read`, { state: readerFrom(location, 'library') })}
          >
            <Icon name="file" size={13} />
            {tr('阅读原文', 'Read paper')}
          </button>
          {paperLibId && (
            <button className="btn btn-ghost sm" onClick={() => navigate(libraryPath(paperLibId, `?paper=${paper.id}`))}>
              <Icon name="book" size={13} />
              {tr('在文献库里看', 'Open in its library')}
            </button>
          )}
          <DotsMenu
            label={tr('这篇论文的更多操作', 'More for this paper')}
            items={[
              ...(paper.wiki_content
                ? [
                    { key: 'reader', icon: 'book' as const, label: tr('全屏阅读解读', 'Read the summary full screen'), onSelect: openReader },
                    { key: 'print', icon: 'download' as const, label: tr('把解读导出为 PDF', 'Export the summary as PDF'), onSelect: openReaderForPrint },
                  ]
                : []),
              ...(arxivUrl || url
                ? [{ key: 'src', icon: 'link' as const, label: arxivUrl ? tr('在 arXiv 打开', 'Open on arXiv') : tr('打开原文页面', 'Open the source page'), onSelect: () => window.open(arxivUrl ?? url ?? '', '_blank', 'noopener') }]
                : []),
            ]}
          />
        </div>
      )}

      {alive && (
        <PaperFacts>
          {paper.relevance_score !== null && paper.relevance_score !== undefined && (
            <PaperFact label={tr('为什么在这里', 'Why it’s here')}>
              {tr(
                `${relevanceWords(paper.relevance_score)}（${paper.relevance_score.toFixed(2)}）`,
                `${relevanceWords(paper.relevance_score)} (${paper.relevance_score.toFixed(2)})`,
              )}
            </PaperFact>
          )}
          <PaperFact label={tr('PDF 与全文', 'PDF & full text')}>
            <PaperFulltextFact libraryId={paperLibId ?? undefined} paperId={paper.id} pdfAvailable={!!paper.pdf_available} doi={paper.doi} />
          </PaperFact>
          <PaperFact label={tr('阅读状态', 'Reading')}>
            <PaperMyMetaRow
              bare
              paperId={paper.id}
              starred={paper.starred ?? false}
              readingStatus={readingStatus}
              detailKey={detailKey}
              invalidateKeys={listKeys}
            />
          </PaperFact>
          <PaperFact label={tr('我的标签', 'My tags')}>
            <PaperMyTagsRow bare paperId={paper.id} myTags={paper.my_tags} detailKey={detailKey} invalidateKeys={listKeys} />
          </PaperFact>
        </PaperFacts>
      )}

      {alive && <ConceptChips concepts={paper.concepts} onOpen={openConcept} />}

      <PaperKeyPoints tldr={tldr} />

      {/* 摘要 + 元信息合成一块：元信息本来就是查证时才看的东西，单独一张卡
          只是多一次点击。样式与「我的笔记」同款，默认收起。 */}
      <MetaFold label={tr('摘要', 'Abstract')}>
        {abstract ? (
          <div style={{ fontSize: 13, lineHeight: 1.7 }}>{abstract}</div>
        ) : (
          <p className="muted" style={{ fontSize: 13, margin: 0 }}>
            {tr('这篇还没有摘要。', 'No abstract for this paper.')}
          </p>
        )}
        <div style={{ marginTop: 14, paddingTop: 12, borderTop: '0.5px solid var(--border)' }}>
        <MetaItem label={tr('arXiv 编号', 'arXiv ID')}>
          {arxivId ? <span className="mono">{arxivId}</span> : <span className="muted">—</span>}
        </MetaItem>
        <MetaItem label="DOI">{doi ? <span className="mono">{doi}</span> : <span className="muted">—</span>}</MetaItem>
        <MetaItem label={tr('年份', 'Year')}>
          {year !== null ? <span className="mono">{year}</span> : <span className="muted">—</span>}
        </MetaItem>
        <MetaItem label={tr('发表于', 'Venue')}>{venue ?? <span className="muted">—</span>}</MetaItem>
        {snapshot.citedByCount !== undefined && (
          <MetaItem label={tr('被引', 'Cited by')}>
            <span className="mono">{snapshot.citedByCount}</span>
          </MetaItem>
        )}
      
        </div>
      </MetaFold>

      {/* —— 我的笔记（只有自己看得到） —— */}
      {alive && (
        <PaperNotesSection paperId={paper.id} noteCount={paper.note_count ?? 0} invalidateKeys={noteKeys} />
      )}

      {/* —— 重要图片画廊（只读：提取/重新提取是库维护动作） —— */}
      {alive && (
        <FiguresSection
          paper={paper}
          readOnly
          defaultCollapsed={hasEmbeddedFigures(paper.wiki_content, figures)}
        />
      )}

      {/* —— Wiki 正文（文献追踪同款 markdown 渲染） —— */}
      {alive && (
        <div style={{ marginTop: 22 }}>
          {paper.wiki_content ? (
            <>
              <div
                className="row gap8"
                style={{ paddingBottom: 10, marginBottom: 16, borderBottom: '0.5px solid var(--border)' }}
              >
                <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)', letterSpacing: '0.04em' }}>
                  {tr('AI 解读', 'AI summary')}
                </span>
                <CompileBadge model={paper.compiled_model} at={paper.compiled_at} />
                <WikiHeaderActions
                  onRead={openReader}
                  onExport={openReaderForPrint}
                  style={{ marginLeft: 'auto' }}
                />
              </div>
              <Markdown source={splitSummary(paper.wiki_content).body || paper.wiki_content} onWikiLink={openConceptByName} renderFigure={renderFigure} />
            </>
          ) : (
            <div
              style={{
                padding: '18px 20px',
                borderRadius: 10,
                border: '1px dashed var(--border-2)',
                textAlign: 'center',
              }}
            >
              <div style={{ fontSize: 13, color: 'var(--text-3)', lineHeight: 1.6 }}>
                {tr('还没有解读', 'No summary yet')}
              </div>
            </div>
          )}
        </div>
      )}

      {/* —— Wiki 快照正文（源论文已删，条目里留存的快照） —— */}
      {!alive && snapshotWiki && (
        <div style={{ marginTop: 22 }}>
          <div
            className="row gap8"
            style={{ paddingBottom: 10, marginBottom: 16, borderBottom: '0.5px solid var(--border)' }}
          >
            <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)', letterSpacing: '0.04em' }}>
              {tr('AI 解读（副本）', 'AI summary (copy)')}
            </span>
          </div>
          <Markdown source={splitSummary(snapshotWiki).body || snapshotWiki} onWikiLink={openConceptByName} renderFigure={renderSnapshotFigure} />
        </div>
      )}

      {readerOpen && alive && (
        <PaperReader
          paper={paper}
          wikiContent={paper.wiki_content}
          renderFigure={renderFigure}
          onWikiLink={openConceptByName}
          autoPrint={readerPrint}
          onClose={() => setReaderOpen(false)}
        />
      )}
    </div>
  );
}
