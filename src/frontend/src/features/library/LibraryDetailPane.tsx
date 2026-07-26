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
import { ScoreRing } from '../../components/ui/ScoreRing';
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
import { AffiliationChips, AuthorLinks, MetaFold, usePoolConceptNav } from '../wiki/shared';
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
      <span style={{ fontSize: 12.5, color: 'var(--text-2)', flex: 1, minWidth: 0, overflowWrap: 'break-word' }}>
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
        {tr('（图片已随源论文删除）', '(figure removed with the source paper)')}
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
    return <div className="empty" style={{ margin: 'auto' }}>{tr('加载论文详情…', 'Loading paper…')}</div>;
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
      {/* —— pills 行 —— */}
      <div className="row gap8 wrap" style={{ marginBottom: 8 }}>
        {venue && (
          <span className="pill sm" style={{ background: 'var(--surface-3)' }}>
            {venue}
          </span>
        )}
        {alive && paper.has_wiki && (
          <span className="pill sm" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
            <Icon name="sparkle" size={11} />
            wiki
          </span>
        )}
        {!alive && (
          <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-3)' }}>
            {tr('源课题已删除，仅保留快照', 'Source topic deleted — snapshot only')}
          </span>
        )}
      </div>

      {/* —— 标题 + 作者 + 机构（都可点：点了按它过滤列表）+ 相关度 —— */}
      <div className="row" style={{ alignItems: 'flex-start', gap: 20 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h1 style={{ fontSize: 20, fontWeight: 680, lineHeight: 1.3, margin: '0 0 6px', letterSpacing: '-0.01em' }}>
            {title}
          </h1>
          <AuthorLinks authors={authors} onFilter={onFilterAuthor} />
          <AffiliationChips affiliations={affiliations} onFilter={onFilterAffiliation} />
        </div>
        {alive && paper.relevance_score !== null && (
          <ScoreRing value={paper.relevance_score} max={1} size={56} label={tr('相关度', 'Relevance')} />
        )}
      </div>

      {/* —— 操作：打开阅读页（仅活体论文）+ 外链 —— */}
      <div className="row gap8 wrap" style={{ marginTop: 14 }}>
        {alive && (
          <button
            className="btn btn-primary sm"
            onClick={() => navigate(`/papers/${paper.id}/read`, { state: readerFrom(location, 'library') })}
          >
            <Icon name="file" size={13} />
            {tr('打开阅读页', 'Open reader')}
          </button>
        )}
        {alive && paper.wiki_content && (
          <button
            className="btn btn-soft sm"
            title={tr('全屏阅览图文介绍，可导出 PDF', 'Full-screen reading view, exportable to PDF')}
            onClick={openReader}
          >
            <Icon name="book" size={13} />
            {tr('阅览模式', 'Reading mode')}
          </button>
        )}
        {alive &&
          (paperLibId ? (
            <button
              className="btn btn-ghost sm"
              onClick={() => navigate(libraryPath(paperLibId, `?paper=${paper.id}`))}
            >
              <Icon name="book" size={13} />
              {tr('去文献库', 'Open library')}
            </button>
          ) : (
            // 手动添加、未纳入任何方向文献库：置灰不可点，hover 说明原因
            <span title={tr('这篇是手动添加的，未纳入公共文献库', 'Manually added — not in any shared library')}>
              <button className="btn btn-ghost sm" disabled style={{ opacity: 0.45, cursor: 'not-allowed' }}>
                <Icon name="book" size={13} />
                {tr('去文献库', 'Open library')}
              </button>
            </span>
          ))}
        {arxivUrl && (
          <a
            className="btn btn-ghost sm"
            href={arxivUrl}
            target="_blank"
            rel="noreferrer noopener"
            style={{ textDecoration: 'none' }}
          >
            <Icon name="link" size={13} />
            arXiv
          </a>
        )}
        {url && !arxivUrl && (
          <a
            className="btn btn-ghost sm"
            href={url}
            target="_blank"
            rel="noreferrer noopener"
            style={{ textDecoration: 'none' }}
          >
            <Icon name="link" size={13} />
            {tr('原文链接', 'Source link')}
          </a>
        )}
      </div>

      {/* —— 个人状态：星标 + 阅读状态（论文还在才有） —— */}
      {alive && (
        <PaperMyMetaRow
          paperId={paper.id}
          starred={paper.starred ?? false}
          readingStatus={readingStatus}
          detailKey={detailKey}
          invalidateKeys={listKeys}
        />
      )}

      {/* —— 我的标签：就地改，只有自己看得到 —— */}
      {/* 库标签的界面入口已移除，个人标签取代了它；后端端点与数据保留。 */}
      {alive && (
        <PaperMyTagsRow
          paperId={paper.id}
          myTags={paper.my_tags}
          detailKey={detailKey}
          invalidateKeys={listKeys}
        />
      )}

      {/* —— frontmatter 风格元信息（默认折叠） —— */}
      <MetaFold>
        <MetaItem label="arxiv_id">
          {arxivId ? <span className="mono">{arxivId}</span> : <span className="muted">—</span>}
        </MetaItem>
        <MetaItem label="doi">{doi ? <span className="mono">{doi}</span> : <span className="muted">—</span>}</MetaItem>
        <MetaItem label={tr('年份', 'year')}>
          {year !== null ? <span className="mono">{year}</span> : <span className="muted">—</span>}
        </MetaItem>
        <MetaItem label={tr('发表于', 'venue')}>{venue ?? <span className="muted">—</span>}</MetaItem>
        {snapshot.citedByCount !== undefined && (
          <MetaItem label={tr('被引', 'cited by')}>
            <span className="mono">{snapshot.citedByCount}</span>
          </MetaItem>
        )}
      </MetaFold>

      {/* —— 概念 chips：点了跳所属课题库的概念页 —— */}
      {alive && <ConceptChips concepts={paper.concepts} onOpen={openConcept} />}

      {/* —— TL;DR —— */}
      {tldr && (
        <div
          style={{
            marginTop: 18,
            padding: '12px 16px',
            borderRadius: 10,
            background: 'var(--accent-soft)',
            fontSize: 13,
            lineHeight: 1.65,
            color: 'var(--text)',
          }}
        >
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--accent-text)', display: 'block', marginBottom: 4 }}>
            TL;DR
          </span>
          {tldr}
        </div>
      )}

      {/* —— 摘要 —— */}
      {abstract && (
        <div style={{ marginTop: 18 }}>
          <div className="mono" style={{ fontSize: 11, color: 'var(--text-4)', letterSpacing: '0.04em', marginBottom: 6 }}>
            {tr('摘要', 'Abstract')}
          </div>
          <div style={{ fontSize: 12.5, lineHeight: 1.7, color: 'var(--text-2)' }}>{abstract}</div>
        </div>
      )}

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
                <span className="mono" style={{ fontSize: 11, color: 'var(--text-4)', letterSpacing: '0.04em' }}>
                  {tr('AI 图文介绍', 'AI intro')}
                </span>
                <CompileBadge model={paper.compiled_model} at={paper.compiled_at} />
                <WikiHeaderActions
                  onRead={openReader}
                  onExport={openReaderForPrint}
                  style={{ marginLeft: 'auto' }}
                />
              </div>
              <Markdown source={paper.wiki_content} onWikiLink={openConceptByName} renderFigure={renderFigure} />
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
              <div style={{ fontSize: 12.5, color: 'var(--text-3)', lineHeight: 1.6 }}>
                {tr('这篇还没有 AI 解读。', 'No AI wiki for this paper yet.')}
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
            <span className="mono" style={{ fontSize: 11, color: 'var(--text-4)', letterSpacing: '0.04em' }}>
              {tr('AI 图文介绍（快照）', 'AI intro (snapshot)')}
            </span>
          </div>
          <Markdown source={snapshotWiki} onWikiLink={openConceptByName} renderFigure={renderSnapshotFigure} />
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
