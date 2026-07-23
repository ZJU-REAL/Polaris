import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import { FigureEmbed, usePaperFigures } from '../../components/ui/FigureGallery';
import { Segmented } from '../../components/ui/Segmented';
import { toast } from '../../components/ui/Toast';
import { Markdown, type WikiLinkHandler } from '../../lib/markdown';
import { api, type PaperRead, type PaperSort } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { ConceptsTab } from '../wiki/ConceptsTab';
import { SearchInput, useDebounced } from '../wiki/shared';

/* ============================================================
   共享文献库只读浏览（P5c 非成员视角）：
   论文（列表 + 检索 + 详情解读）/ 概念 两个页签，全部走 /libraries 读端点；
   没有任何管理入口，收藏/笔记等个人操作去阅读页做。
   ============================================================ */

type BrowseTab = 'papers' | 'concepts';
type SearchScope = 'keyword' | 'semantic';

const PAGE_SIZE = 20;

function authorsLine(p: PaperRead): string {
  return (p.authors ?? [])
    .map((a) => (typeof a === 'string' ? a : a.name))
    .filter(Boolean)
    .join(', ');
}

function PaperRow({ p, active, onClick }: { p: PaperRead; active: boolean; onClick: () => void }) {
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
      <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.35 }}>{p.title}</div>
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
        <button className="btn btn-primary sm" onClick={() => navigate(`/papers/${paper.id}/read`)}>
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
  useEffect(() => setPage(1), [q, sort, scope]);

  const semantic = !!q && scope === 'semantic';
  const listQuery = useQuery({
    queryKey: ['lib-papers', libraryId, q, sort, page],
    queryFn: () =>
      api.listLibraryPapers(libraryId, { q: q || undefined, sort, page, size: PAGE_SIZE }),
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
            <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)', flexShrink: 0 }}>
              {total ? tr(`${total} 篇`, `${total}`) : ''}
            </span>
          </div>
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
              title={q ? tr('没有匹配的论文', 'No matching papers') : tr('这个库还是空的', 'This library is empty')}
              desc={q ? tr('换个关键词试试。', 'Try a different keyword.') : undefined}
            />
          ) : (
            items.map((p) => (
              <PaperRow key={p.id} p={p} active={p.id === selectedId} onClick={() => onSelect(p.id)} />
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
          ]}
          value={tab}
          onChange={setTab}
        />
        <span style={{ fontSize: 11.5, color: 'var(--text-4)' }}>
          {tr('公共文献库 · 所有人可读', 'Shared library · readable by everyone')}
        </span>
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
            onWikiLink={setPendingConceptName}
          />
        ) : (
          <ConceptsTab
            libraryId={libraryId}
            selectedId={conceptId}
            onSelect={setConceptId}
            onOpenPaper={(id) => {
              setPaperId(id);
              setTab('papers');
            }}
            onWikiLink={setPendingConceptName}
          />
        )}
      </div>
    </>
  );
}
