import { Suspense, lazy, useCallback, useEffect, useState } from 'react';
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import { FigureEmbed, usePaperFigures } from '../../components/ui/FigureGallery';
import { Segmented } from '../../components/ui/Segmented';
import { toast } from '../../components/ui/Toast';
import { Markdown, type WikiLinkHandler } from '../../lib/markdown';
import { api, type PaperRead, type PaperSort, type ReadingStatus } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { ConceptsTab } from '../wiki/ConceptsTab';
import { LibraryChatTab } from '../wiki/LibraryChatTab';
import { NotesTab } from '../wiki/NotesTab';
import { GovernanceTab } from '../wiki/GovernanceTab';
import { IngestTab } from '../wiki/IngestTab';
import {
  AdvancedPanel,
  AdvancedToggle,
  FilterInput,
  SearchInput,
  YearRangeField,
  parseYear,
  saveBlob,
  useDebounced,
} from '../wiki/shared';
import { READING_STATUS, readerFrom } from '../reading/shared';
import { AddToButton } from '../library/AddToPopover';

// 图谱体量大且非默认视图：按需加载（与 WikiWorkbench 一致）
const GraphTab = lazy(() => import('../wiki/GraphTab').then((m) => ({ default: m.GraphTab })));

/* ============================================================
   共享文献库只读浏览（P5c 非成员视角）：
   论文库 / 概念库 / 图谱 / 文献对话 / 笔记 / 文献库配置（只读）/ 建库与同步（只读），
   全部走 /libraries 读端点；没有任何管理入口，收藏/笔记等个人操作去阅读页做。
   ============================================================ */

type BrowseTab = 'papers' | 'concepts' | 'graph' | 'chat' | 'notes' | 'govern' | 'ingest';
type SearchScope = 'keyword' | 'semantic';

const PAGE_SIZE = 20;

function authorsLine(p: PaperRead): string {
  return (p.authors ?? [])
    .map((a) => (typeof a === 'string' ? a : a.name))
    .filter(Boolean)
    .join(', ');
}

function PaperRow({
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
        padding: '11px 16px',
        cursor: 'pointer',
        borderBottom: '0.5px solid var(--border)',
        background: active ? 'var(--accent-soft)' : 'transparent',
        borderLeft: active ? '2px solid var(--accent)' : '2px solid transparent',
        transition: 'background 0.12s',
      }}
    >
      <div className="row gap8" style={{ alignItems: 'flex-start' }}>
        {/* 占位常驻：切换多选时行内容不左右跳 */}
        <input
          type="checkbox"
          checked={checked}
          onClick={(e) => e.stopPropagation()}
          onChange={onToggleCheck}
          title={tr('选中后可批量导出引用', 'Select for bulk citation export')}
          style={{
            width: 13,
            height: 13,
            margin: '2px 0 0',
            flexShrink: 0,
            accentColor: 'var(--accent)',
            cursor: 'pointer',
            visibility: selectMode ? 'visible' : 'hidden',
          }}
        />
        <div style={{ flex: 1, minWidth: 0, fontSize: 13, fontWeight: 600, lineHeight: 1.35 }}>{p.title}</div>
        <AddToButton paperId={p.id} />
      </div>
      <div className="row gap8" style={{ marginTop: 4, fontSize: 11, color: 'var(--text-3)' }}>
        {p.has_wiki && (
          <span className="pill sm" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
            <Icon name="sparkle" size={10} />
            wiki
          </span>
        )}
        <span
          style={{ flex: 1, minWidth: 0, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}
          title={authorsLine(p)}
        >
          {authorsLine(p)}
        </span>
        {p.year !== null && <span className="mono" style={{ flexShrink: 0 }}>{p.year}</span>}
      </div>
      {p.tldr && (
        <div
          style={{
            fontSize: 11.5,
            color: 'var(--text-3)',
            marginTop: 3,
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          }}
        >
          {p.tldr}
        </div>
      )}
    </div>
  );
}

function PaperDetailPane({ paperId, onWikiLink }: { paperId: string; onWikiLink: WikiLinkHandler }) {
  const navigate = useNavigate();
  const location = useLocation();
  const { data: paper, isLoading, isError } = useQuery({
    queryKey: ['paper', paperId],
    queryFn: () => api.getPaper(paperId),
    retry: false,
  });

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

  const arxivUrl = paper.arxiv_id ? `https://arxiv.org/abs/${paper.arxiv_id}` : paper.url;

  return (
    <div className="scroll fadeup" key={paper.id} style={{ overflowY: 'auto', flex: 1, padding: '26px 32px 60px' }}>
      <div className="row gap8 wrap" style={{ marginBottom: 8 }}>
        {paper.venue && (
          <span className="pill sm" style={{ background: 'var(--surface-3)' }}>{paper.venue}</span>
        )}
        {paper.year !== null && <span className="pill sm" style={{ background: 'var(--surface-3)' }}>{paper.year}</span>}
        {arxivUrl && (
          <a className="pill sm" href={arxivUrl} target="_blank" rel="noreferrer" style={{ background: 'var(--surface-3)' }}>
            <Icon name="link" size={11} />
            {paper.arxiv_id ? `arXiv:${paper.arxiv_id}` : tr('原文链接', 'Source')}
          </a>
        )}
      </div>
      <h1 style={{ fontSize: 21, fontWeight: 680, lineHeight: 1.3, margin: '2px 0 6px', letterSpacing: '-0.01em' }}>
        {paper.title}
      </h1>
      {authorsLine(paper) && (
        <div style={{ fontSize: 12.5, color: 'var(--text-3)', marginBottom: 14 }}>{authorsLine(paper)}</div>
      )}
      <div className="row gap8" style={{ marginBottom: 18 }}>
        <button className="btn btn-primary sm" onClick={() => navigate(`/papers/${paper.id}/read`, { state: readerFrom(location, 'wiki') })}>
          <Icon name="book" size={13} />
          {tr('打开阅读页', 'Open reading page')}
        </button>
        <span style={{ fontSize: 11.5, color: 'var(--text-4)' }}>
          {tr('原文 PDF、配图、笔记和收藏都在阅读页里', 'PDF, figures, notes and saving live on the reading page')}
        </span>
      </div>

      {paper.wiki_content ? (
        <Markdown source={paper.wiki_content} onWikiLink={onWikiLink} renderFigure={renderFigure} />
      ) : (
        <>
          {paper.abstract && (
            <div className="card card-pad" style={{ background: 'var(--surface-2)' }}>
              <div className="row gap8" style={{ marginBottom: 8 }}>
                <Icon name="file" size={14} style={{ color: 'var(--accent)' }} />
                <span style={{ fontSize: 12, fontWeight: 700 }}>{tr('摘要', 'Abstract')}</span>
              </div>
              <p style={{ fontSize: 13.5, lineHeight: 1.7, margin: 0 }}>{paper.abstract}</p>
            </div>
          )}
          <div className="empty" style={{ padding: 20 }}>
            {tr('这篇还没有中文解读。', 'No wiki for this paper yet.')}
          </div>
        </>
      )}
    </div>
  );
}

function PapersPane({
  libraryId,
  selectedId,
  onSelect,
  onWikiLink,
}: {
  libraryId: string;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onWikiLink: WikiLinkHandler;
}) {
  const [qInput, setQInput] = useState('');
  const q = useDebounced(qInput.trim());
  const [scope, setScope] = useState<SearchScope>('keyword');
  const [sort, setSort] = useState<PaperSort>('relevance');
  const [page, setPage] = useState(1);

  // 多选（批量导出引用）：默认关闭，底部「多选」按钮开启后行首出现复选框（只读浏览不加删除）
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  useEffect(() => {
    setSelected(new Set());
    setSelectMode(false);
  }, [libraryId, q]);

  const bulkExportMutation = useMutation({
    mutationFn: () => api.downloadLibraryCitations(libraryId, { format: 'bibtex', ids: [...selected] }),
    onSuccess: (blob) => {
      saveBlob(blob, 'polaris-library-citations.bib');
      toast(tr(`已导出 ${selected.size} 篇的 BibTeX`, `Exported BibTeX for ${selected.size} papers`), 'ok');
    },
    onError: (e) =>
      toast(`${tr('导出失败：', 'Export failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  // —— 高级检索（作者 / 机构 / 年份区间 / 阅读状态 / 星标） ——
  const [advOpen, setAdvOpen] = useState(false);
  const [advAuthor, setAdvAuthor] = useState('');
  const [advAffiliation, setAdvAffiliation] = useState('');
  const [advYearFrom, setAdvYearFrom] = useState('');
  const [advYearTo, setAdvYearTo] = useState('');
  const [advReading, setAdvReading] = useState<'' | ReadingStatus>('');
  const [advStarred, setAdvStarred] = useState(false);
  const author = useDebounced(advAuthor.trim());
  const affiliation = useDebounced(advAffiliation.trim());
  const yearFrom = parseYear(advYearFrom);
  const yearTo = parseYear(advYearTo);
  const advActive = !!(author || affiliation || yearFrom || yearTo || advReading || advStarred);

  useEffect(
    () => setPage(1),
    [q, sort, scope, author, affiliation, yearFrom, yearTo, advReading, advStarred],
  );

  const semantic = !!q && scope === 'semantic';
  const listQuery = useQuery({
    queryKey: [
      'lib-papers', libraryId, q, sort, page,
      author, affiliation, yearFrom, yearTo, advReading, advStarred,
    ],
    queryFn: () =>
      api.listLibraryPapersFull(libraryId, {
        q: q || undefined,
        sort,
        page,
        size: PAGE_SIZE,
        author: author || undefined,
        affiliation: affiliation || undefined,
        published_from: yearFrom ? `${yearFrom}-01-01T00:00:00Z` : undefined,
        published_to: yearTo ? `${yearTo}-12-31T23:59:59Z` : undefined,
        reading_status: advReading || undefined,
        starred: advStarred || undefined,
      }),
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

  return (
    <div className="split">
      <div className="split-list">
        <div style={{ padding: '12px 14px 10px', borderBottom: '0.5px solid var(--border)' }}>
          <div className="row gap8">
            <SearchInput
              value={qInput}
              onChange={setQInput}
              placeholder={tr('搜库内论文：标题 / 摘要 / 解读…', 'Search papers: title / abstract / wiki…')}
            />
            <AdvancedToggle
              open={advOpen}
              active={advActive}
              onToggle={() => setAdvOpen((o) => !o)}
              title={tr('高级检索：作者 / 机构 / 年份 / 阅读状态 / 星标', 'Advanced search: author / affiliation / year / reading status / starred')}
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
                        setAdvReading('');
                        setAdvStarred(false);
                      }
                    : undefined
                }
              >
                <div className="row gap8">
                  <FilterInput value={advAuthor} onChange={setAdvAuthor} placeholder={tr('作者姓名…', 'Author name…')} />
                  <FilterInput
                    value={advAffiliation}
                    onChange={setAdvAffiliation}
                    placeholder={tr('发表机构…', 'Affiliation…')}
                    title={tr(
                      '需要论文元数据带有机构信息（入库时自动从 OpenAlex 补充）',
                      'Needs affiliation metadata (auto-filled from OpenAlex on ingest)',
                    )}
                  />
                </div>
                <YearRangeField
                  label={tr('发表年份', 'Year')}
                  from={advYearFrom}
                  to={advYearTo}
                  onFrom={setAdvYearFrom}
                  onTo={setAdvYearTo}
                />
                <div className="row gap6" style={{ alignItems: 'center' }}>
                  <select
                    className="input"
                    style={{ height: 26, fontSize: 11.5, flex: 1, minWidth: 0, padding: '0 6px' }}
                    value={advReading}
                    onChange={(e) => setAdvReading(e.target.value as '' | ReadingStatus)}
                    title={tr('按阅读状态过滤', 'Filter by reading status')}
                  >
                    <option value="">{tr('读没读', 'Read?')}</option>
                    {READING_STATUS.map((m) => (
                      <option key={m.v} value={m.v}>
                        {tr(m.label, m.en)}
                      </option>
                    ))}
                  </select>
                  <label className="row gap6" style={{ cursor: 'pointer', userSelect: 'none', fontSize: 11.5, color: 'var(--text-2)' }}>
                    <input
                      type="checkbox"
                      checked={advStarred}
                      onChange={(e) => setAdvStarred(e.target.checked)}
                      style={{ width: 14, height: 14, accentColor: 'var(--accent)' }}
                    />
                    {tr('仅看星标', 'Starred only')}
                  </label>
                </div>
              </AdvancedPanel>
            </div>
          )}
          <div className="row gap6 wrap" style={{ marginTop: 10 }}>
            <span className={`chip${scope === 'keyword' ? ' on' : ''}`} onClick={() => setScope('keyword')}>
              {tr('关键词', 'Keyword')}
            </span>
            <span className={`chip${scope === 'semantic' ? ' on' : ''}`} onClick={() => setScope('semantic')}>
              {tr('语义检索', 'Semantic')}
            </span>
            <span style={{ flex: 1 }} />
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
              title={q || advActive ? tr('没有匹配的论文', 'No matching papers') : tr('这个库还是空的', 'This library is empty')}
              desc={q || advActive ? tr('换个关键词或调整高级条件试试。', 'Try a different keyword or adjust the filters.') : undefined}
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
            title={tr('开启后列表出现复选框，可批量导出引用', 'Show checkboxes for bulk citation export')}
            onClick={() => {
              setSelectMode((m) => !m);
              setSelected(new Set());
            }}
          >
            <Icon name="check" size={13} />
            {selectMode ? tr(`已选 ${selected.size}`, `${selected.size} selected`) : tr('多选', 'Select')}
          </button>
          {selectMode && (
            <button
              className="btn btn-ghost sm"
              disabled={selected.size === 0 || bulkExportMutation.isPending}
              onClick={() => bulkExportMutation.mutate()}
            >
              <Icon name="download" size={12} />
              {tr('导出 BibTeX', 'Export BibTeX')}
            </button>
          )}
        </div>
      </div>

      <div className="split-detail">
        {selectedId ? (
          <PaperDetailPane paperId={selectedId} onWikiLink={onWikiLink} />
        ) : (
          <div className="empty" style={{ margin: 'auto' }}>
            {tr('从左侧选择一篇论文', 'Pick a paper from the list')}
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

  // [[概念名]] → 概念 id（库端点解析）
  const resolveQuery = useQuery({
    queryKey: ['lib-concept-resolve', libraryId, pendingConceptName],
    queryFn: () => api.listLibraryConcepts(libraryId, { q: pendingConceptName ?? '' }),
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
    const name = pendingConceptName.toLowerCase();
    const hit = resolveQuery.data.find((c) => c.name.toLowerCase() === name) ?? resolveQuery.data[0];
    if (hit) {
      setConceptId(hit.id);
      setTab('concepts');
    } else {
      toast(tr(`概念 ${pendingConceptName} 尚未入库`, `Concept ${pendingConceptName} is not in the library yet`), 'info');
    }
    setPendingConceptName(null);
  }, [pendingConceptName, resolveQuery.data, resolveQuery.isError]);

  return (
    <>
      <div className="row" style={{ marginBottom: 14, justifyContent: 'space-between' }}>
        <Segmented<BrowseTab>
          options={[
            { v: 'papers', label: tr('论文库', 'Papers') },
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
          {ingestQuery.data?.running_voyage_id && tab !== 'ingest' && (
            <span
              className="pill hoverable"
              style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}
              onClick={() => navigate(`/voyages/${ingestQuery.data?.running_voyage_id ?? ''}`)}
            >
              <span className="dot pulse" />
              {tr('文献任务运行中 →', 'Literature task running →')}
            </span>
          )}
          <span style={{ fontSize: 11.5, color: 'var(--text-4)' }}>
            {tr('公共文献库 · 所有人可读', 'Shared library · readable by everyone')}
          </span>
        </div>
      </div>

      <div
        className="card"
        style={{ overflow: 'hidden', display: 'flex', flexDirection: 'column', flex: 1, minHeight: 480 }}
      >
        {tab === 'papers' ? (
          <PapersPane
            libraryId={libraryId}
            selectedId={paperId}
            onSelect={setPaperId}
            onWikiLink={onWikiLink}
          />
        ) : tab === 'concepts' ? (
          <ConceptsTab
            libraryId={libraryId}
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
          <LibraryChatTab libraryId={libraryId} onOpenPaper={goPaper} onWikiLink={onWikiLink} />
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
