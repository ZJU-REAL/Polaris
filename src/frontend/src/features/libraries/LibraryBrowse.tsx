import { Suspense, lazy, memo, useCallback, useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import {
  FigureEmbed,
  FiguresSection,
  hasEmbeddedFigures,
  usePaperFigures,
} from '../../components/ui/FigureGallery';
import { CompileBadge } from '../../components/ui/CompileBadge';
import { citationExportItems, ExportDropdown } from '../../components/ui/ExportDropdown';
import { RelevanceBar } from '../../components/ui/RelevanceBar';
import { ScoreRing } from '../../components/ui/ScoreRing';
import { PaperStatusPill } from '../../components/ui/StatusPill';
import { Segmented } from '../../components/ui/Segmented';
import { toast } from '../../components/ui/Toast';
import { Markdown, type WikiLinkHandler } from '../../lib/markdown';
import { fmtTime } from '../../lib/format';
import {
  api,
  type CitationFormat,
  type PaperRead,
  type PaperSort,
  type PaperStatusFilter,
  type ReadingStatus,
} from '../../lib/api';
import { tr } from '../../lib/i18n';
import {
  ConceptChips,
  PaperMyMetaRow,
  PaperMyTagChips,
  PaperMyTagsRow,
  PaperNotesSection,
  WikiHeaderActions,
} from '../shared/PaperDetailBlocks';
import { ConceptsTab } from '../wiki/ConceptsTab';
import { LibraryChatTab } from '../wiki/LibraryChatTab';
import { NotesTab } from '../wiki/NotesTab';
import { GovernanceTab } from '../wiki/GovernanceTab';
import { IngestTab } from '../wiki/IngestTab';
import { PaperReader } from '../wiki/PaperReader';
import {
  AdvancedPanel,
  AdvancedToggle,
  AffiliationChips,
  AuthorLinks,
  FilterInput,
  MetaItem,
  MyTagField,
  ReadingStatusField,
  SearchInput,
  SemanticSwitch,
  YearRangeField,
  parseYear,
  pickConceptByName,
  saveBlob,
  useDebounced,
} from '../wiki/shared';
import { ReadingDot, readerFrom } from '../reading/shared';
import { AddToButton } from '../library/AddToPopover';

// 图谱体量大且非默认视图：按需加载（与 WikiWorkbench 一致）
const GraphTab = lazy(() => import('../wiki/GraphTab').then((m) => ({ default: m.GraphTab })));

/* ============================================================
   共享文献库只读浏览（P5c 非成员视角）：
   论文库 / 概念库 / 图谱 / 文献对话 / 笔记 / 文献库配置（只读）/ 建库与同步（只读），
   全部走 /libraries 读端点；没有任何管理入口，收藏/笔记等个人操作去阅读页做。
   ============================================================ */

type BrowseTab = 'papers' | 'concepts' | 'graph' | 'chat' | 'notes' | 'govern' | 'ingest';

const PAGE_SIZE = 20;

/** 列表范围筛选（口径同文献工作台：全部 = 已纳入的文献）；收在高级检索面板里 */
type ViewFilter = 'all' | 'compiled' | 'starred';

// 模块级常量存 zh/en 两份文案，渲染处再 tr（import 时求值不会随语言切换更新）
const VIEW_FILTERS: { v: ViewFilter; zh: string; en: string; hintZh: string; hintEn: string }[] = [
  { v: 'all', zh: '全部', en: 'All', hintZh: '已纳入知识库的全部文献', hintEn: 'Every paper included in the library' },
  { v: 'compiled', zh: '已编译', en: 'Compiled', hintZh: 'AI 已精读编译出介绍', hintEn: 'Papers the AI has compiled an intro for' },
  { v: 'starred', zh: '已星标', en: 'Starred', hintZh: '我加了星标的文献', hintEn: 'Papers I starred' },
];

function viewQuery(view: ViewFilter): { status: PaperStatusFilter; starred?: boolean } {
  if (view === 'compiled') return { status: 'compiled_any' };
  if (view === 'starred') return { status: 'library', starred: true };
  return { status: 'library' };
}

/* memo：父组件（大量筛选/选中 state）任一变更都会触发全列表重渲染。
   忽略函数 props 的比较是安全的：两个 handler 只捕获稳定引用与 p.id。 */
const PaperRow = memo(function PaperRow({
  p,
  active,
  checked,
  selectMode,
  onClick,
  onToggleCheck,
}: {
  p: PaperRead;
  active: boolean;
  checked: boolean;
  selectMode: boolean;
  onClick: () => void;
  onToggleCheck: () => void;
}) {
  return (
    <div
      onClick={onClick}
      style={{
        padding: '12px 16px',
        cursor: 'pointer',
        borderBottom: '0.5px solid var(--border)',
        background: active ? 'var(--accent-soft)' : 'transparent',
        borderLeft: active ? '2px solid var(--accent)' : '2px solid transparent',
        transition: 'background 0.12s',
      }}
    >
      <div className="row gap8" style={{ marginBottom: 5 }}>
        {/* 占位常驻：切换多选时行内容不左右跳 */}
        <input
          type="checkbox"
          checked={checked}
          onClick={(e) => e.stopPropagation()}
          onChange={onToggleCheck}
          style={{
            width: 13,
            height: 13,
            margin: 0,
            flexShrink: 0,
            accentColor: 'var(--accent)',
            cursor: 'pointer',
            visibility: selectMode ? 'visible' : 'hidden',
          }}
        />
        {p.starred && <Icon name="starFill" size={11} style={{ color: 'var(--warn-tx)', flexShrink: 0 }} />}
        <span className="mono" style={{ fontSize: 10.5, color: active ? 'var(--accent-text)' : 'var(--text-3)' }}>
          {p.arxiv_id ?? p.venue ?? '—'}
        </span>
        {p.year !== null && (
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>
            {p.year}
          </span>
        )}
        {p.has_wiki && <Icon name="sparkle" size={11} style={{ color: 'var(--accent)' }} />}
        <span style={{ marginLeft: 'auto' }}>
          <RelevanceBar value={p.relevance_score} />
        </span>
      </div>
      <div className="row gap8" style={{ alignItems: 'flex-start' }}>
        <div style={{ flex: 1, minWidth: 0, fontSize: 13, fontWeight: 600, lineHeight: 1.35, color: 'var(--text)' }}>
          {p.title}
        </div>
        <AddToButton paperId={p.id} />
      </div>
      <div className="row gap8" style={{ marginTop: 6 }}>
        <PaperStatusPill status={p.status} sm />
        <ReadingDot status={p.reading_status} />
        <PaperMyTagChips myTags={p.my_tags} />
        {(p.note_count ?? 0) > 0 && (
          <span
            className="row"
            style={{ gap: 3, fontSize: 10.5, color: 'var(--text-3)', flexShrink: 0 }}
            title={tr(`${p.note_count} 条笔记`, `${p.note_count} notes`)}
          >
            <Icon name="pen" size={10} />
            {p.note_count}
          </span>
        )}
        {p.tldr && (
          <span
            style={{
              flex: 1,
              minWidth: 0,
              fontSize: 11.5,
              color: 'var(--text-3)',
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >
            {p.tldr}
          </span>
        )}
      </div>
    </div>
  );
}, (prev, next) =>
  prev.p === next.p && prev.active === next.active && prev.checked === next.checked && prev.selectMode === next.selectMode,
);

function PaperDetailPane({
  libraryId,
  paperId,
  onWikiLink,
  onOpenConcept,
  onFilterAuthor,
  onFilterAffiliation,
}: {
  libraryId: string;
  paperId: string;
  onWikiLink: WikiLinkHandler;
  /** 点概念 chip → 跳概念库 */
  onOpenConcept: (id: string) => void;
  /** 点作者名 → 列表按该作者过滤 */
  onFilterAuthor: (name: string) => void;
  /** 点机构 chip → 列表按该机构过滤 */
  onFilterAffiliation: (name: string) => void;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const [abstractOpen, setAbstractOpen] = useState(false);
  const [readerOpen, setReaderOpen] = useState(false);
  const [readerPrint, setReaderPrint] = useState(false);
  const openReader = useCallback(() => {
    setReaderPrint(false);
    setReaderOpen(true);
  }, []);
  const openReaderForPrint = useCallback(() => {
    setReaderPrint(true);
    setReaderOpen(true);
  }, []);

  // 库作用域取详情：锁定本库那份成员行，相关度/状态/wiki 不会串到该论文在别的库的副本
  const detailKey = useMemo(() => ['paper', libraryId, paperId], [libraryId, paperId]);
  const { data: paper, isLoading, isError } = useQuery({
    queryKey: detailKey,
    queryFn: () => api.getLibraryPaper(libraryId, paperId),
    retry: false,
  });

  // 星标 / 阅读状态、笔记改动后要刷新的列表（库列表 + 库内搜索 + 库笔记本）
  const listKeys = useMemo(
    () => [['lib-papers', libraryId], ['lib-search', libraryId]],
    [libraryId],
  );
  const noteKeys = useMemo(
    () => [detailKey, ['lib-papers', libraryId], ['lib-search', libraryId], ['project-notes', libraryId]],
    [detailKey, libraryId],
  );

  // 换论文时收起本地开合状态（面板不随选中项重挂载，得自己收）
  useEffect(() => {
    setAbstractOpen(false);
    setReaderOpen(false);
  }, [paperId]);

  // 正文 ![[fig:N]] 嵌入图（与文献追踪同款渲染；图片端点对全员开放）
  const figures = usePaperFigures(paper);
  const renderFigure = useCallback(
    (n: number) => {
      const fig = figures.find((f) => f.index === n);
      return fig && paper ? <FigureEmbed paperId={paper.id} fig={fig} /> : null;
    },
    [figures, paper],
  );

  if (isLoading) return <div className="empty">{tr('加载论文详情…', 'Loading paper…')}</div>;
  if (isError || !paper) {
    return (
      <EmptyState
        compact
        icon="x"
        title={tr('无法加载论文详情', 'Failed to load paper')}
        desc={tr('后端不可用或该论文不存在。', 'Backend unavailable or the paper does not exist.')}
      />
    );
  }

  const arxivUrl = paper.arxiv_id ? `https://arxiv.org/abs/${paper.arxiv_id}` : null;
  const relevance = paper.relevance_score;
  const starred = paper.starred ?? false;
  const readingStatus: ReadingStatus = paper.reading_status ?? 'unread';

  return (
    <div className="scroll fadeup" key={paper.id} style={{ overflowY: 'auto', flex: 1, padding: '26px 32px 60px' }}>
      {/* —— 元数据头 —— */}
      <div className="row" style={{ alignItems: 'flex-start', gap: 20 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="row gap8 wrap" style={{ marginBottom: 8 }}>
            <PaperStatusPill status={paper.status} sm />
            {paper.venue && (
              <span className="pill sm" style={{ background: 'var(--surface-3)' }}>
                {paper.venue}
              </span>
            )}
            {paper.has_wiki && (
              <span className="pill sm" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
                <Icon name="sparkle" size={11} />
                wiki
              </span>
            )}
            {paper.pdf_available && (
              <span className="pill sm" style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>
                <Icon name="file" size={11} />
                PDF
              </span>
            )}
            {/* 笔记数不在这里重复：下方「我的笔记」区标题已带计数 */}
          </div>
          <h1 style={{ fontSize: 20, fontWeight: 680, lineHeight: 1.3, margin: '0 0 6px', letterSpacing: '-0.01em' }}>
            {paper.title}
          </h1>
          <AuthorLinks authors={paper.authors} onFilter={onFilterAuthor} />
          <AffiliationChips affiliations={paper.affiliations} onFilter={onFilterAffiliation} />
        </div>
        {relevance !== null && <ScoreRing value={relevance} max={1} size={56} label={tr('相关度', 'Relevance')} />}
      </div>

      {/* —— 操作（只读视角：阅读 / 阅览 / 原文链接） —— */}
      <div className="row gap8 wrap" style={{ marginTop: 14 }}>
        <button
          className="btn btn-primary sm"
          onClick={() => navigate(`/papers/${paper.id}/read`, { state: readerFrom(location, 'wiki') })}
        >
          <Icon name="file" size={13} />
          {tr('阅读原文', 'Read original')}
        </button>
        {paper.has_wiki && paper.wiki_content && (
          <button
            className="btn btn-soft sm"
            title={tr('全屏阅览图文介绍，可导出 PDF', 'Full-screen reading view, exportable to PDF')}
            onClick={openReader}
          >
            <Icon name="book" size={13} />
            {tr('阅览模式', 'Reading mode')}
          </button>
        )}
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
        {paper.url && !arxivUrl && (
          <a
            className="btn btn-ghost sm"
            href={paper.url}
            target="_blank"
            rel="noreferrer noopener"
            style={{ textDecoration: 'none' }}
          >
            <Icon name="link" size={13} />
            {tr('原文链接', 'Source link')}
          </a>
        )}
      </div>

      {/* —— 个人状态：星标 + 阅读状态（只读库里也是我自己的记录） —— */}
      <PaperMyMetaRow
        paperId={paper.id}
        starred={starred}
        readingStatus={readingStatus}
        detailKey={detailKey}
        invalidateKeys={listKeys}
      />

      {/* —— 我的标签：就地改，只有自己看得到 —— */}
      {/* 库标签的界面入口已移除，个人标签取代了它；后端端点与数据保留。 */}
      <PaperMyTagsRow
        paperId={paper.id}
        myTags={paper.my_tags}
        detailKey={detailKey}
        invalidateKeys={listKeys}
      />

      {/* —— frontmatter 风格元数据卡 —— */}
      <div className="card card-pad" style={{ margin: '18px 0 0', background: 'var(--surface-2)', padding: '14px 18px' }}>
        <MetaItem label="arxiv_id">
          {paper.arxiv_id ? <span className="mono">{paper.arxiv_id}</span> : <span className="muted">—</span>}
        </MetaItem>
        <MetaItem label="doi">{paper.doi ? <span className="mono">{paper.doi}</span> : <span className="muted">—</span>}</MetaItem>
        <MetaItem label="published">
          {paper.published_at ? <span className="mono">{paper.published_at.slice(0, 10)}</span> : <span className="muted">—</span>}
        </MetaItem>
        <MetaItem label="relevance">
          {relevance !== null ? (
            <RelevanceBar value={relevance} width={140} />
          ) : (
            <span className="muted">{tr('未打分', 'not scored')}</span>
          )}
        </MetaItem>
        <MetaItem label={tr('入库时间', 'added at')}>
          <span className="mono">{fmtTime(paper.created_at)}</span>
        </MetaItem>
        <MetaItem label={tr('编译时间', 'compiled at')}>
          {paper.compiled_at ? (
            <span className="mono">{fmtTime(paper.compiled_at)}</span>
          ) : (
            <span className="muted">{tr('未编译', 'not compiled')}</span>
          )}
        </MetaItem>
      </div>

      {/* —— 概念 chips（过多时折叠） —— */}
      <ConceptChips concepts={paper.concepts} onOpen={(c) => onOpenConcept(c.id)} />

      {/* —— 摘要（折叠） —— */}
      {paper.abstract && (
        <div className="card" style={{ marginTop: 18, overflow: 'hidden' }}>
          <div
            className="row"
            onClick={() => setAbstractOpen((o) => !o)}
            style={{ padding: '11px 16px', cursor: 'pointer', justifyContent: 'space-between', userSelect: 'none' }}
          >
            <span style={{ fontSize: 12.5, fontWeight: 650 }}>{tr('摘要', 'Abstract')}</span>
            <Icon
              name="chevDown"
              size={14}
              style={{ color: 'var(--text-3)', transform: abstractOpen ? 'rotate(180deg)' : 'none', transition: 'transform .15s' }}
            />
          </div>
          {abstractOpen && (
            <div style={{ padding: '0 16px 14px', fontSize: 12.5, lineHeight: 1.7, color: 'var(--text-2)' }}>
              {paper.abstract}
            </div>
          )}
        </div>
      )}

      {/* —— TL;DR —— */}
      {paper.tldr && (
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
          {paper.tldr}
        </div>
      )}

      {/* —— 我的笔记（个人维度，只读浏览也能写） —— */}
      <PaperNotesSection paperId={paper.id} noteCount={paper.note_count ?? 0} invalidateKeys={noteKeys} />

      {/* —— 重要图片画廊（只读：不给提取/重新提取入口，那是管理员操作） —— */}
      <FiguresSection paper={paper} readOnly defaultCollapsed={hasEmbeddedFigures(paper.wiki_content, figures)} />

      {/* —— Wiki 正文（markdown，含 ![[fig:N]] 嵌入图） —— */}
      <div style={{ marginTop: 22 }}>
        {paper.wiki_content ? (
          <>
            <div
              className="row"
              style={{
                justifyContent: 'space-between',
                alignItems: 'center',
                paddingBottom: 10,
                marginBottom: 16,
                borderBottom: '0.5px solid var(--border)',
              }}
            >
              <div className="row gap8">
                <span className="mono" style={{ fontSize: 11, color: 'var(--text-4)', letterSpacing: '0.04em' }}>
                  {tr('AI 图文介绍', 'AI intro')}
                </span>
                <CompileBadge model={paper.compiled_model} at={paper.compiled_at} />
              </div>
              <WikiHeaderActions onRead={openReader} onExport={openReaderForPrint} />
            </div>
            <Markdown source={paper.wiki_content} onWikiLink={onWikiLink} renderFigure={renderFigure} />
          </>
        ) : (
          <div className="col" style={{ alignItems: 'center', gap: 8, padding: '18px 20px' }}>
            <div className="empty" style={{ padding: 0 }}>
              {tr('这篇还没有 AI 解读。', 'No AI wiki for this paper yet.')}
            </div>
          </div>
        )}
      </div>

      {readerOpen && (
        <PaperReader
          paper={paper}
          wikiContent={paper.wiki_content}
          renderFigure={renderFigure}
          onWikiLink={onWikiLink}
          onFilterAuthor={(name) => {
            setReaderOpen(false);
            onFilterAuthor(name);
          }}
          autoPrint={readerPrint}
          onClose={() => setReaderOpen(false)}
        />
      )}
    </div>
  );
}

function PapersPane({
  libraryId,
  selectedId,
  onSelect,
  onWikiLink,
  onOpenConcept,
}: {
  libraryId: string;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onWikiLink: WikiLinkHandler;
  onOpenConcept: (id: string) => void;
}) {
  const [qInput, setQInput] = useState('');
  const q = useDebounced(qInput.trim());
  // 语义检索开关（true=按意思检索，false=关键词字面匹配）
  const [semanticOn, setSemanticOn] = useState(false);
  const [sort, setSort] = useState<PaperSort>('relevance');
  const [page, setPage] = useState(1);
  // 范围筛选（全部 / 已编译 / 已星标）：收在高级检索面板里，口径同文献工作台
  const [view, setView] = useState<ViewFilter>('all');

  // 多选（批量导出引用）：默认关闭，底部「多选」按钮开启后行首出现复选框（只读浏览不加删除）
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  useEffect(() => {
    setSelected(new Set());
    setSelectMode(false);
  }, [libraryId, q, view]);

  const bulkExportMutation = useMutation({
    mutationFn: (format: CitationFormat) => api.downloadLibraryCitations(libraryId, { format, ids: [...selected] }),
    onSuccess: (blob, format) => {
      saveBlob(blob, format === 'bibtex' ? 'polaris-library-citations.bib' : 'polaris-library-citations.json');
      toast(tr(`已导出 ${selected.size} 篇`, `Exported ${selected.size} papers`), 'ok');
    },
    onError: (e) =>
      toast(`${tr('导出失败：', 'Export failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  // —— 高级检索（作者 / 机构 / 年份区间 / 我的标签 / 阅读状态 / 星标） ——
  const [advOpen, setAdvOpen] = useState(false);
  const [advAuthor, setAdvAuthor] = useState('');
  const [advAffiliation, setAdvAffiliation] = useState('');
  const [advYearFrom, setAdvYearFrom] = useState('');
  const [advYearTo, setAdvYearTo] = useState('');
  const [advMyTag, setAdvMyTag] = useState('');
  const [advReading, setAdvReading] = useState<'' | ReadingStatus>('');
  const author = useDebounced(advAuthor.trim());
  const affiliation = useDebounced(advAffiliation.trim());
  const yearFrom = parseYear(advYearFrom);
  const yearTo = parseYear(advYearTo);
  // 范围也算一个高级条件（「已星标」就是原来的星标筛选，不再单列）
  const advActive = !!(author || affiliation || yearFrom || yearTo || advMyTag || advReading) || view !== 'all';

  useEffect(
    () => setPage(1),
    [q, sort, semanticOn, view, author, affiliation, yearFrom, yearTo, advMyTag, advReading],
  );

  // 点作者/机构 → 列表只留匹配的论文（走已有的高级检索），其余条件重置并展开面板
  const applyAdvFilter = useCallback(
    (patch: { author?: string; affiliation?: string }) => {
      setSemanticOn(false);
      setQInput('');
      setAdvAuthor(patch.author ?? '');
      setAdvAffiliation(patch.affiliation ?? '');
      setAdvYearFrom('');
      setAdvYearTo('');
      setAdvMyTag('');
      setAdvReading('');
      setView('all');
      setAdvOpen(true);
      onSelect('');
      if (patch.author) {
        toast(tr(`已筛选作者：${patch.author}`, `Filtered by author: ${patch.author}`), 'info');
      } else if (patch.affiliation) {
        toast(tr(`已筛选机构：${patch.affiliation}`, `Filtered by affiliation: ${patch.affiliation}`), 'info');
      }
    },
    [onSelect],
  );
  const filterByAuthor = useCallback((name: string) => applyAdvFilter({ author: name }), [applyAdvFilter]);
  const filterByAffiliation = useCallback((name: string) => applyAdvFilter({ affiliation: name }), [applyAdvFilter]);

  const semantic = !!q && semanticOn;
  const listQuery = useQuery({
    queryKey: [
      'lib-papers', libraryId, view, advMyTag, q, sort, page,
      author, affiliation, yearFrom, yearTo, advReading,
    ],
    queryFn: () => {
      const vq = viewQuery(view);
      return api.listLibraryPapersFull(libraryId, {
        status: vq.status,
        starred: vq.starred || undefined,
        my_tag: advMyTag || undefined,
        q: q || undefined,
        sort,
        page,
        size: PAGE_SIZE,
        author: author || undefined,
        affiliation: affiliation || undefined,
        published_from: yearFrom ? `${yearFrom}-01-01T00:00:00Z` : undefined,
        published_to: yearTo ? `${yearTo}-12-31T23:59:59Z` : undefined,
        reading_status: advReading || undefined,
      });
    },
    enabled: !semantic,
    retry: false,
  });
  const semQuery = useQuery({
    queryKey: ['lib-search', libraryId, q],
    queryFn: () => api.searchLibrary(libraryId, { q, mode: 'semantic' }),
    enabled: semantic,
    retry: false,
  });

  const items: PaperRead[] = semantic ? semQuery.data?.papers ?? [] : listQuery.data?.items ?? [];
  const total = semantic ? items.length : listQuery.data?.total ?? 0;
  const pages = semantic ? 1 : Math.max(1, Math.ceil(total / PAGE_SIZE));
  const isLoading = semantic ? semQuery.isLoading : listQuery.isLoading;
  const isError = semantic ? semQuery.isError : listQuery.isError;

  // 首条自动选中
  const firstId = items[0]?.id ?? null;
  useEffect(() => {
    if (!selectedId && firstId) onSelect(firstId);
  }, [selectedId, firstId, onSelect]);

  const hasFilter = !!q || advActive;
  const filterDisabled = semantic ? { opacity: 0.45, pointerEvents: 'none' as const } : undefined;

  return (
    <div className="split">
      <div className="split-list">
        <div style={{ padding: '12px 14px 10px', borderBottom: '0.5px solid var(--border)' }}>
          <div className="row gap8">
            <SearchInput
              value={qInput}
              onChange={setQInput}
              placeholder={
                semanticOn
                  ? tr('语义检索（自然语言描述）…', 'Semantic search (natural language)…')
                  : tr('搜库内论文：标题 / 摘要 / 解读…', 'Search papers: title / abstract / wiki…')
              }
            />
            <SemanticSwitch checked={semanticOn} onChange={setSemanticOn} />
            <AdvancedToggle
              open={advOpen}
              active={advActive}
              onToggle={() => setAdvOpen((o) => !o)}
              title={tr('高级检索', 'Advanced search')}
            />
            <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)', flexShrink: 0 }}>
              {total ? tr(`${total} 篇`, `${total}`) : ''}
            </span>
          </div>
          {advOpen && (
            <div style={semantic ? { opacity: 0.45, pointerEvents: 'none' } : undefined}>
              <AdvancedPanel
                onClear={
                  advActive
                    ? () => {
                        setAdvAuthor('');
                        setAdvAffiliation('');
                        setAdvYearFrom('');
                        setAdvYearTo('');
                        setAdvMyTag('');
                        setAdvReading('');
                        setView('all');
                      }
                    : undefined
                }
              >
                {/* 范围：原来面板外的那排 chip 收进来；「已星标」即原先单列的星标筛选 */}
                <div className="row gap6 wrap" style={{ alignItems: 'center' }}>
                  <span style={{ width: 52, flexShrink: 0, fontSize: 11, color: 'var(--text-3)' }}>
                    {tr('范围', 'Scope')}
                  </span>
                  {VIEW_FILTERS.map((f) => (
                    <span
                      key={f.v}
                      className={`chip${view === f.v ? ' on' : ''}`}
                      title={tr(f.hintZh, f.hintEn)}
                      onClick={() => setView(f.v)}
                    >
                      {tr(f.zh, f.en)}
                    </span>
                  ))}
                </div>
                <div className="row gap8">
                  <FilterInput value={advAuthor} onChange={setAdvAuthor} placeholder={tr('作者姓名…', 'Author name…')} />
                  <FilterInput
                    value={advAffiliation}
                    onChange={setAdvAffiliation}
                    placeholder={tr('发表机构…', 'Affiliation…')}
                    title={tr('需要论文元数据带有机构信息', 'Needs affiliation metadata on the paper')}
                  />
                </div>
                <YearRangeField
                  label={tr('发表年份', 'Year')}
                  from={advYearFrom}
                  to={advYearTo}
                  onFrom={setAdvYearFrom}
                  onTo={setAdvYearTo}
                />
                <MyTagField value={advMyTag} onChange={setAdvMyTag} />
                <ReadingStatusField value={advReading} onChange={setAdvReading} />
              </AdvancedPanel>
            </div>
          )}
          {/* 排序（语义检索按相关度返回，排序无效 → 置灰） */}
          <div
            className="row gap6 wrap"
            style={{ marginTop: 10, justifyContent: 'flex-end', ...filterDisabled }}
          >
            <span
              className={`chip${sort === 'relevance' ? ' on' : ''}`}
              onClick={() => setSort('relevance')}
            >
              {tr('按相关性', 'Relevance')}
            </span>
            <span
              className={`chip${sort === '-published_at' ? ' on' : ''}`}
              onClick={() => setSort('-published_at')}
            >
              {tr('按发表时间', 'Newest')}
            </span>
          </div>
        </div>

        <div className="scroll" style={{ overflowY: 'auto', flex: 1 }}>
          {isLoading ? (
            <div className="empty">{tr('加载论文…', 'Loading papers…')}</div>
          ) : isError ? (
            <EmptyState
              compact
              icon="x"
              title={tr('无法加载论文列表', 'Failed to load papers')}
              desc={tr('后端不可用或接口尚未就绪，稍后重试。', 'Backend unavailable — try again later.')}
            />
          ) : items.length === 0 ? (
            <EmptyState
              compact
              icon="book"
              title={hasFilter ? tr('没有匹配的论文', 'No matching papers') : tr('这个库还是空的', 'This library is empty')}
              desc={hasFilter ? tr('换个关键词或调整筛选条件试试。', 'Try a different keyword or adjust the filters.') : undefined}
            />
          ) : (
            items.map((p) => (
              <PaperRow
                key={p.id}
                p={p}
                active={p.id === selectedId}
                checked={selected.has(p.id)}
                selectMode={selectMode}
                onClick={() => onSelect(p.id)}
                onToggleCheck={() =>
                  setSelected((old) => {
                    const next = new Set(old);
                    if (next.has(p.id)) next.delete(p.id);
                    else next.add(p.id);
                    return next;
                  })
                }
              />
            ))
          )}
        </div>

        {pages > 1 && (
          <div className="row gap8" style={{ padding: '8px 14px', borderTop: '0.5px solid var(--border)', justifyContent: 'center' }}>
            <button className="btn btn-ghost sm" disabled={page <= 1} onClick={() => setPage((x) => x - 1)}>
              {tr('上一页', 'Prev')}
            </button>
            <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>
              {page} / {pages}
            </span>
            <button className="btn btn-ghost sm" disabled={page >= pages} onClick={() => setPage((x) => x + 1)}>
              {tr('下一页', 'Next')}
            </button>
          </div>
        )}

        {/* —— 底部固定操作栏：多选 + 导出引用（只读浏览不含删除） —— */}
        <div
          className="row gap8"
          style={{ padding: '9px 14px', borderTop: '0.5px solid var(--border)', flexShrink: 0 }}
        >
          <button
            className={'btn sm ' + (selectMode ? 'btn-primary' : 'btn-ghost')}
            title={tr('批量导出引用', 'Bulk citation export')}
            onClick={() => {
              setSelectMode((m) => !m);
              setSelected(new Set());
            }}
          >
            <Icon name="check" size={13} />
            {selectMode
              ? tr(`已选 ${selected.size} 篇`, `${selected.size} selected`)
              : tr('多选', 'Select')}
          </button>
          {selectMode && (
            <ExportDropdown
              sm
              placement="up"
              align="left"
              busy={bulkExportMutation.isPending}
              disabled={selected.size === 0}
              items={citationExportItems((format) => bulkExportMutation.mutate(format))}
            />
          )}
        </div>
      </div>

      <div className="split-detail">
        {selectedId ? (
          <PaperDetailPane
            libraryId={libraryId}
            paperId={selectedId}
            onWikiLink={onWikiLink}
            onOpenConcept={onOpenConcept}
            onFilterAuthor={filterByAuthor}
            onFilterAffiliation={filterByAffiliation}
          />
        ) : (
          <div className="empty" style={{ margin: 'auto' }}>
            {tr('从列表中选择一篇论文', 'Pick a paper from the list')}
          </div>
        )}
      </div>
    </div>
  );
}

export function LibraryBrowse({ libraryId }: { libraryId: string }) {
  const navigate = useNavigate();
  const [tab, setTab] = useState<BrowseTab>('papers');
  const [paperId, setPaperId] = useState<string | null>(null);
  const [conceptId, setConceptId] = useState<string | null>(null);
  const [pendingConceptName, setPendingConceptName] = useState<string | null>(null);

  // 切库重置选中态
  useEffect(() => {
    setPaperId(null);
    setConceptId(null);
    setPendingConceptName(null);
  }, [libraryId]);

  // 深链 ?paper=<id> / ?concept=<名称>（处理后清参数；只读视角忽略其他参数）
  const [searchParams, setSearchParams] = useSearchParams();
  useEffect(() => {
    const p = searchParams.get('paper');
    const c = searchParams.get('concept');
    if (!p && !c && ![...searchParams.keys()].length) return;
    if (p) {
      setPaperId(p);
      setTab('papers');
    } else if (c) {
      setPendingConceptName(c);
    }
    setSearchParams({}, { replace: true });
  }, [searchParams, setSearchParams]);

  // 建库与同步（只读）状态：与工作台同 queryKey，运行中加快轮询
  const ingestQuery = useQuery({
    queryKey: ['ingest-state', libraryId],
    queryFn: () => api.getLibraryIngestState(libraryId),
    retry: false,
    refetchInterval: (query) => (query.state.data?.running_voyage_id ? 5_000 : 60_000),
  });

  const goPaper = useCallback((id: string) => {
    setPaperId(id);
    setTab('papers');
  }, []);
  const goConcept = useCallback((id: string) => {
    setConceptId(id);
    setTab('concepts');
  }, []);
  const onWikiLink = useCallback((name: string) => {
    setPendingConceptName(name);
  }, []);

  // [[概念名]] → 概念 id：概念全平台一份，走平台级按名查（不限本库），命中后仍留在本库视角
  const resolveQuery = useQuery({
    queryKey: ['concept-resolve', pendingConceptName],
    queryFn: () => api.lookupConcept(pendingConceptName ?? ''),
    enabled: !!pendingConceptName,
    retry: false,
  });
  useEffect(() => {
    if (!pendingConceptName) return;
    if (resolveQuery.isError) {
      toast(tr('概念解析失败（后端不可用）', 'Concept lookup failed (backend unavailable)'), 'error');
      setPendingConceptName(null);
      return;
    }
    if (!resolveQuery.data) return;
    const hit = pickConceptByName(resolveQuery.data, pendingConceptName);
    if (hit) {
      setConceptId(hit.id);
      setTab('concepts');
    } else {
      toast(
        tr(`概念「${pendingConceptName}」还没入库`, `“${pendingConceptName}” is not in the knowledge base yet`),
        'info',
      );
    }
    setPendingConceptName(null);
  }, [pendingConceptName, resolveQuery.data, resolveQuery.isError]);

  // 论文库计数口径 = 库内（相关性达标）；旧后端无 library 字段时退回 total
  const paperTotal = ingestQuery.data?.paper_counts?.library ?? ingestQuery.data?.paper_counts?.total;

  return (
    <>
      <div className="row" style={{ marginBottom: 14, justifyContent: 'space-between' }}>
        <Segmented<BrowseTab>
          options={[
            { v: 'papers', label: `${tr('论文库', 'Papers')}${paperTotal !== undefined ? ` · ${paperTotal}` : ''}` },
            { v: 'concepts', label: tr('概念库', 'Concepts') },
            { v: 'graph', label: tr('图谱', 'Graph') },
            { v: 'chat', label: tr('文献对话', 'Chat') },
            { v: 'notes', label: tr('笔记', 'Notes') },
            { v: 'govern', label: tr('文献库配置', 'Library config') },
            // 建库与同步放到最后一个标签
            { v: 'ingest', label: tr('建库与同步', 'Ingest & sync') },
          ]}
          value={tab}
          onChange={setTab}
        />
        <div className="row gap8">
          {/* 无权打开任务详情时只报状态、不给跳转（点了会 404） */}
          {ingestQuery.data?.running_voyage_id && tab !== 'ingest' && (
            ingestQuery.data.can_open_running_voyage ? (
              <span
                className="pill hoverable"
                style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}
                onClick={() => navigate(`/voyages/${ingestQuery.data?.running_voyage_id ?? ''}`)}
              >
                <span className="dot pulse" />
                {tr('文献任务运行中 →', 'Literature task running →')}
              </span>
            ) : (
              <span className="pill" style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>
                <span className="dot pulse" />
                {tr('文献任务运行中', 'Literature task running')}
              </span>
            )
          )}
          <span style={{ fontSize: 11.5, color: 'var(--text-4)' }}>
            {tr('公共文献库 · 所有人可读', 'Shared library · readable by everyone')}
          </span>
        </div>
      </div>

      <div
        className="card split-card"
      >
        {tab === 'papers' ? (
          <PapersPane
            libraryId={libraryId}
            selectedId={paperId}
            onSelect={setPaperId}
            onWikiLink={onWikiLink}
            onOpenConcept={goConcept}
          />
        ) : tab === 'concepts' ? (
          <ConceptsTab
            libraryId={libraryId}
            canManage={false}
            selectedId={conceptId}
            onSelect={setConceptId}
            onOpenPaper={goPaper}
            onWikiLink={onWikiLink}
          />
        ) : tab === 'graph' ? (
          <Suspense fallback={<div className="skel" style={{ flex: 1, margin: 16 }} />}>
            <GraphTab libraryId={libraryId} onOpenPaper={goPaper} onOpenConcept={goConcept} />
          </Suspense>
        ) : tab === 'chat' ? (
          <LibraryChatTab libraryId={libraryId} canManage={false} onOpenPaper={goPaper} onWikiLink={onWikiLink} />
        ) : tab === 'notes' ? (
          <NotesTab libraryId={libraryId} />
        ) : tab === 'govern' ? (
          <GovernanceTab libraryId={libraryId} readOnly />
        ) : (
          <IngestTab
            libraryId={libraryId}
            state={ingestQuery.data}
            stateError={ingestQuery.isError}
            stateLoading={ingestQuery.isLoading}
            readOnly
          />
        )}
      </div>
    </>
  );
}
