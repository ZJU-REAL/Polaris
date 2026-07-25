import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { SelectMenu } from '../../components/ui/SelectMenu';
import { Segmented } from '../../components/ui/Segmented';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import {
  api,
  type DirectionLibrarySummary,
  type PaperRead,
  type ReadingStatus,
  type ShelfImportInput,
  type ShelfItemRead,
  type ShelfSort,
  type ShelfWikiSource,
} from '../../lib/api';
import { tr } from '../../lib/i18n';
import { useProject } from '../../app/project';
import { libraryPath } from '../libraries/hooks';
import {
  AdvancedPanel,
  AdvancedToggle,
  FilterInput,
  parseYear,
  saveBlob,
  SearchInput,
  useDebounced,
  YearRangeField,
} from '../wiki/shared';
import { AddPaperModal } from './AddPaperModal';
import { PaperProgressModal } from '../library/PaperProgressModal';
import { ShelfChatTab } from './ShelfChatTab';
import { ShelfDetailPane, WikiBadge } from './ShelfDetailPane';

/* ============================================================
   /t/:topicId/research — 课题「相关研究」书架。
   双栏主从布局对齐「我的文献库」：左栏紧凑论文行（排序 / 状态
   过滤 / 计数），右栏选中论文的完整详情（wiki 渲染、课题备注、
   就地动作）；添加统一收进「添加论文」弹窗（从文献库 / 手动）。
   入架同时自动收藏进「我的文献库」；移出书架不动个人库。
   ============================================================ */

// 后端单页上限 100；排序/关键词/筛选走后端，wiki_source 状态过滤在页内完成
const PAGE_SIZE = 100;

type ShelfFilter = 'all' | ShelfWikiSource;
/** 页面级 tab：书架列表 / 相关研究对话 */
type PageTab = 'list' | 'chat';
/** 阅读状态筛选：空串=不限；其余透传给后端 reading_status。 */
type ReadingFilter = '' | ReadingStatus;
/** 搜索作用域：关键词（后端书架过滤）/ 语义检索（课题语料向量召回）。 */
type SearchScope = 'keyword' | 'semantic';

/** 语义检索命中的 ScoredPaper（课题语料，未必已入书架）映射成书架行需要的最小字段。
    note / wiki_content / snapshot_at / source_library_id 语义结果里没有，按缺省填；
    wiki_source 用 has_wiki 粗略推断（有解读→库版徽标，没有→暂无）。行/详情只作展示用，
    真正的备注/移出/生成走 (pid, paper_id) 幂等接口，不依赖这些映射字段。 */
function scoredToShelf(p: PaperRead & { score?: number | null }): ShelfItemRead {
  return {
    paper_id: p.id,
    title: p.title,
    authors: p.authors,
    year: p.year,
    venue: p.venue,
    arxiv_id: p.arxiv_id,
    doi: p.doi,
    url: p.url,
    tldr: p.tldr,
    note: null,
    wiki_source: p.has_wiki ? 'live' : 'none',
    wiki_content: null,
    snapshot_at: null,
    source_library_id: null,
    added_at: p.created_at,
  };
}

// 模块级常量不调 tr()：保留 zh/en 字段，渲染处再 tr
const SORTS: { v: ShelfSort; zh: string; en: string }[] = [
  { v: 'added', zh: '按添加时间', en: 'By added' },
  { v: 'year', zh: '按年份', en: 'By year' },
  { v: 'relevance', zh: '按相关度', en: 'By relevance' },
  { v: 'title', zh: '按标题', en: 'By title' },
];
const FILTERS: { v: ShelfFilter; zh: string; en: string }[] = [
  { v: 'all', zh: '全部状态', en: 'All statuses' },
  { v: 'live', zh: '库版解读', en: 'Library wiki' },
  { v: 'personal', zh: '个人版解读', en: 'Personal wiki' },
  { v: 'snapshot', zh: '快照解读', en: 'Snapshot wiki' },
  { v: 'none', zh: '暂无解读', en: 'No wiki' },
];
const READING_FILTERS: { v: ReadingFilter; zh: string; en: string }[] = [
  { v: '', zh: '全部', en: 'All' },
  { v: 'unread', zh: '未读', en: 'Unread' },
  { v: 'reading', zh: '在读', en: 'Reading' },
  { v: 'read', zh: '已读', en: 'Read' },
];

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/* ---------------- 左栏列表行（同「我的文献库」EntryRow 版式） ---------------- */

function ShelfRow({
  item,
  active,
  checked,
  selectMode,
  onSelect,
  onToggleCheck,
}: {
  item: ShelfItemRead;
  active: boolean;
  checked: boolean;
  selectMode: boolean;
  onSelect: () => void;
  onToggleCheck: () => void;
}) {
  const authors = item.authors.map((a) => a.name).join(', ');
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onSelect}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onSelect();
        }
      }}
      style={{
        padding: '12px 16px',
        borderBottom: '0.5px solid var(--border)',
        cursor: 'pointer',
        background: active ? 'var(--accent-soft)' : 'transparent',
        borderLeft: active ? '2px solid var(--accent)' : '2px solid transparent',
        transition: 'background 0.12s',
      }}
    >
      {/* 顶部 mono 元信息行：复选框（多选态常驻占位）+ 编号/venue + 年份；右侧解读状态徽标 */}
      <div className="row gap8" style={{ marginBottom: 5 }}>
        {/* 占位常驻：切换多选时行内容不左右跳（对齐文献库 PapersTab） */}
        <input
          type="checkbox"
          checked={checked}
          onClick={(e) => e.stopPropagation()}
          onChange={onToggleCheck}
          title={tr('选中后可批量导出引用', 'Select for bulk citation export')}
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
        <span
          className="mono"
          style={{
            fontSize: 10.5,
            color: active ? 'var(--accent-text)' : 'var(--text-3)',
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          }}
        >
          {item.arxiv_id ?? item.venue ?? '—'}
        </span>
        {item.year !== null && (
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', flexShrink: 0 }}>
            {item.year}
          </span>
        )}
        <span style={{ marginLeft: 'auto' }} />
        <WikiBadge source={item.wiki_source} compact />
      </div>

      <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.35, color: 'var(--text)' }}>{item.title}</div>

      {(authors || item.venue) && (
        <div
          title={authors}
          style={{
            fontSize: 11.5,
            color: 'var(--text-3)',
            marginTop: 3,
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          }}
        >
          {authors}
          {authors && item.venue && item.arxiv_id ? ` · ${item.venue}` : ''}
        </div>
      )}

      {/* 备注摘要一行：写过备注才显示 */}
      {item.note && (
        <div className="row gap6" style={{ marginTop: 5, alignItems: 'flex-start' }}>
          <Icon name="pen" size={11} style={{ marginTop: 2, flexShrink: 0, color: 'var(--text-4)' }} />
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
            {item.note}
          </span>
        </div>
      )}
    </div>
  );
}

/* ---------------- 关联文献库区块（书架之上） ---------------- */

/** 非 active 库的小状态徽标（待审批 / 已驳回）。 */
function LibStatusBadge({ status }: { status: DirectionLibrarySummary['status'] }) {
  if (status === 'active') return null;
  const cfg =
    status === 'pending'
      ? { zh: '待审批', en: 'Pending', bg: 'var(--warn-bg)', tx: 'var(--warn-tx)' }
      : { zh: '已驳回', en: 'Rejected', bg: 'var(--danger-bg)', tx: 'var(--danger-tx)' };
  return (
    <span className="pill sm" style={{ background: cfg.bg, color: cfg.tx, flexShrink: 0, marginLeft: 2 }}>
      {tr(cfg.zh, cfg.en)}
    </span>
  );
}

/** 课题关联的文献库（语料来源）：库名 + 各库论文数 + 进库入口。
    总篇数用工作台同源的 stats.papers_total（并集去重口径），不做 per-library 相加。 */
function LinkedLibrariesBar({
  pid,
  libs,
  corpusTotal,
  loading,
  onNavigate,
}: {
  pid: string;
  libs: DirectionLibrarySummary[];
  /** 并集语料总篇数（stats.papers_total，与工作台一致）；未就绪时 null */
  corpusTotal: number | null;
  loading: boolean;
  onNavigate: (path: string) => void;
}) {
  const linkedCount = libs.length;
  const totalPart = corpusTotal !== null ? tr(` · 共 ${corpusTotal} 篇`, ` · ${corpusTotal} papers`) : '';
  const title =
    linkedCount === 0
      ? tr('关联文献库', 'Linked libraries')
      : tr(`关联文献库 · ${linkedCount} 个${totalPart}`, `Linked libraries · ${linkedCount}${totalPart}`);

  return (
    <div className="card card-pad" style={{ marginBottom: 16, flexShrink: 0 }}>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: linkedCount === 0 ? 0 : 12 }}>
        <span className="section-h">
          <Icon name="book" size={15} style={{ color: 'var(--accent)' }} />
          {title}
        </span>
        <button className="btn btn-ghost sm" onClick={() => onNavigate(`/projects/${pid}`)}>
          <Icon name="link" size={12} />
          {tr('管理关联库', 'Linked libraries')}
        </button>
      </div>

      {loading ? (
        <div className="row gap8">
          <div className="skel" style={{ height: 30, width: 180 }} />
          <div className="skel" style={{ height: 30, width: 150 }} />
        </div>
      ) : linkedCount === 0 ? (
        <div className="row gap10" style={{ alignItems: 'center', flexWrap: 'wrap' }}>
          <span style={{ fontSize: 12.5, color: 'var(--text-3)', lineHeight: 1.5, flex: 1, minWidth: 220 }}>
            {tr(
              '这个课题还没关联文献库——先去关联，才有可挑选的语料。',
              'This topic has no linked libraries yet — link one to get a corpus to pick from.',
            )}
          </span>
          <div className="row gap8">
            <button className="btn btn-primary sm" onClick={() => onNavigate(`/projects/${pid}`)}>
              <Icon name="link" size={13} />
              {tr('去关联', 'Link a library')}
            </button>
            <button className="btn btn-ghost sm" onClick={() => onNavigate('/libraries')}>
              {tr('浏览全部文献库', 'Browse all libraries')}
            </button>
          </div>
        </div>
      ) : (
        <>
          <div className="row gap8" style={{ flexWrap: 'wrap' }}>
            {libs.map((lib) => (
              <button
                key={lib.id}
                className="btn btn-soft sm"
                style={{ maxWidth: 300 }}
                title={lib.name}
                onClick={() => onNavigate(libraryPath(lib.id))}
              >
                <Icon name="book" size={12} style={{ flexShrink: 0 }} />
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>
                  {lib.name}
                </span>
                <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)', flexShrink: 0 }}>
                  {tr(`${lib.paper_count} 篇`, `${lib.paper_count}`)}
                </span>
                <LibStatusBadge status={lib.status} />
              </button>
            ))}
          </div>
          <div style={{ fontSize: 11.5, color: 'var(--text-3)', lineHeight: 1.55, marginTop: 10 }}>
            {tr(
              '这些库的论文并集就是课题的可用语料；下面「相关研究」是你从中手挑出来的一小撮，两个数不一样是正常的。',
              'The union of these libraries is the corpus available to this topic; the related work below is the handful you hand-picked — the two counts differ by design.',
            )}
          </div>
        </>
      )}
    </div>
  );
}

/* ---------------- 页面 ---------------- */

export function ResearchPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { currentProjectId } = useProject();
  const pid = currentProjectId ?? '';

  const [tab, setTab] = useState<PageTab>('list');
  const [page, setPage] = useState(1);
  const [sort, setSort] = useState<ShelfSort>('added');
  const [filter, setFilter] = useState<ShelfFilter>('all');
  const [selId, setSelId] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  // 个人补充入架后若后端返回 task_id，弹出分阶段处理进度
  const [progress, setProgress] = useState<{ taskId: string; title: string } | null>(null);

  // 关键词 + 高级检索条件（走后端）
  const [qInput, setQInput] = useState('');
  const q = useDebounced(qInput.trim());
  const [scope, setScope] = useState<SearchScope>('keyword');
  // 多选导出：默认关闭，底部「多选」按钮开启后行首出现复选框
  const [selectMode, setSelectMode] = useState(false);
  const [checkedIds, setCheckedIds] = useState<Set<string>>(new Set());
  const [advOpen, setAdvOpen] = useState(false);
  const [author, setAuthor] = useState('');
  const [affiliation, setAffiliation] = useState('');
  const [yearFrom, setYearFrom] = useState('');
  const [yearTo, setYearTo] = useState('');
  const [readingStatus, setReadingStatus] = useState<ReadingFilter>('');
  const [starred, setStarred] = useState(false);

  const advActive =
    !!author.trim() ||
    !!affiliation.trim() ||
    !!yearFrom.trim() ||
    !!yearTo.trim() ||
    readingStatus !== '' ||
    starred;
  // 是否有任何后端筛选（用于空态文案区分「没添加」vs「没匹配」）
  const hasServerFilter = !!q || advActive;

  const clearAdvanced = () => {
    setAuthor('');
    setAffiliation('');
    setYearFrom('');
    setYearTo('');
    setReadingStatus('');
    setStarred(false);
  };

  useEffect(() => {
    setPage(1);
    setSelId(null);
    setFilter('all');
    setQInput('');
    setScope('keyword');
    clearAdvanced();
  }, [pid]);

  // 后端筛选/排序变化时回到第一页
  useEffect(() => {
    setPage(1);
  }, [q, sort, author, affiliation, yearFrom, yearTo, readingStatus, starred]);

  // 切换课题 / 搜索词 / 过滤 / 作用域时退出多选（对齐文献库 PapersTab）
  useEffect(() => {
    setCheckedIds(new Set());
    setSelectMode(false);
  }, [pid, q, scope, filter, sort]);

  // 语义检索：有查询词且作用域为语义时激活；结果替换列表、不分页、置灰其余过滤
  const semantic = !!q && scope === 'semantic';

  const shelfQuery = useQuery({
    queryKey: [
      'shelf',
      pid,
      page,
      sort,
      q,
      author.trim(),
      affiliation.trim(),
      yearFrom.trim(),
      yearTo.trim(),
      readingStatus,
      starred,
    ],
    queryFn: () =>
      api.listShelf(pid, {
        page,
        size: PAGE_SIZE,
        sort,
        q: q || undefined,
        author: author.trim() || undefined,
        affiliation: affiliation.trim() || undefined,
        year_from: parseYear(yearFrom),
        year_to: parseYear(yearTo),
        reading_status: readingStatus || undefined,
        starred: starred || undefined,
      }),
    enabled: !!pid && !semantic,
    retry: false,
    placeholderData: keepPreviousData,
  });
  // 语义检索：复用课题作用域搜索端点（向量召回 + rerank）；结果是课题语料（未必已入书架）
  const semQuery = useQuery({
    queryKey: ['shelf-search', pid, q],
    queryFn: () => api.searchProject(pid, { q, mode: 'semantic', limit: 30 }),
    enabled: !!pid && semantic,
    retry: false,
  });
  const semItems = useMemo<ShelfItemRead[]>(
    () => (semQuery.data?.papers ?? []).map(scoredToShelf),
    [semQuery.data],
  );
  // 后端 provider 不支持向量时会回退关键词，如实提示
  const semFallback = semantic && semQuery.data?.mode_used === 'keyword';
  const idsQuery = useQuery({
    queryKey: ['shelf-ids', pid],
    queryFn: () => api.listShelfIds(pid),
    enabled: !!pid,
    retry: false,
  });
  const shelvedIds = new Set(idsQuery.data?.paper_ids ?? []);

  // 课题关联的文献库（语料来源）：缓存键与工作台/课题设置共享（['sourceLibraries', pid]）
  const sourceLibrariesQuery = useQuery({
    queryKey: ['sourceLibraries', pid],
    queryFn: () => api.getSourceLibraries(pid),
    enabled: !!pid,
    retry: false,
  });
  // 并集语料总篇数：与工作台完全同源（['stats', pid] → papers_total），不做 per-library 相加
  const statsQuery = useQuery({
    queryKey: ['stats', pid],
    queryFn: () => api.getStats(pid),
    enabled: !!pid,
    retry: false,
  });
  const libs = useMemo<DirectionLibrarySummary[]>(() => sourceLibrariesQuery.data ?? [], [sourceLibrariesQuery.data]);
  const corpusTotal = statsQuery.data?.papers_total ?? null;

  // 「文献库入口」目标：正好 1 个关联库→进那个库；多个→课题设置关联区；0 个→全部文献库列表。
  // （不再指向隐式库 topicLib——新模型里课题无单一隐式库）
  const firstLib = libs[0];
  const libEntryHref =
    libs.length === 1 && firstLib ? libraryPath(firstLib.id) : libs.length > 1 ? `/projects/${pid}` : '/libraries';
  const libEntryLabel =
    libs.length === 1
      ? tr('去文献库', 'Open library')
      : libs.length > 1
        ? tr('管理关联库', 'Linked libraries')
        : tr('浏览全部文献库', 'Browse libraries');

  const data = shelfQuery.data;
  const items = useMemo(() => data?.items ?? [], [data]);
  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.size)) : 1;

  // 后端已排序/筛选；wiki_source 状态过滤留在页内完成（后端无此参数）
  const visible = useMemo(
    () => (filter === 'all' ? items : items.filter((i) => i.wiki_source === filter)),
    [items, filter],
  );

  // 列表数据源：语义态用检索结果，否则用书架可见项
  const rows = semantic ? semItems : visible;

  // 选中项：优先手动选择；不在可见列表（被过滤/移出/切模式）时退回第一条
  const selected = rows.find((i) => i.paper_id === selId) ?? rows[0] ?? null;
  // 语义命中的论文可能尚未入书架：详情面板据此切换「移出」/「加入相关研究」
  const selectedOnShelf = selected ? shelvedIds.has(selected.paper_id) : false;

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['shelf', pid] });
    void queryClient.invalidateQueries({ queryKey: ['shelf-ids', pid] });
    // 入架同步收藏进个人库
    void queryClient.invalidateQueries({ queryKey: ['library'] });
    void queryClient.invalidateQueries({ queryKey: ['library-state'] });
  };

  const addMutation = useMutation({
    mutationFn: (paperId: string) => api.addToShelf(pid, { paper_id: paperId }),
    onSuccess: (item) => {
      toast(tr('已加入相关研究', 'Added to related work'), 'ok');
      setSelId(item.paper_id);
      invalidate();
    },
    onError: (e) => toast(`${tr('添加失败：', 'Failed to add: ')}${errText(e)}`, 'error'),
  });

  const importMutation = useMutation({
    mutationFn: (input: ShelfImportInput) => api.importToShelf(pid, input),
    onSuccess: (item) => {
      setSelId(item.paper_id);
      invalidate();
      if (item.task_id) {
        // 还需后处理：弹进度弹窗替代成功 toast，避免重复打扰
        setProgress({ taskId: item.task_id, title: item.title });
      } else {
        toast(tr('已添加到相关研究', 'Added to related work'), 'ok');
      }
    },
    onError: (e) => toast(`${tr('添加失败：', 'Failed to add: ')}${errText(e)}`, 'error'),
  });

  const noteMutation = useMutation({
    mutationFn: ({ paperId, note }: { paperId: string; note: string | null }) =>
      api.updateShelfNote(pid, paperId, note),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['shelf', pid] }),
    onError: (e) => toast(`${tr('备注保存失败：', 'Failed to save note: ')}${errText(e)}`, 'error'),
  });

  const removeMutation = useMutation({
    mutationFn: (paperId: string) => api.removeFromShelf(pid, paperId),
    onSuccess: (_d, paperId) => {
      toast(tr('已移出相关研究（个人库收藏保留）', 'Removed (still saved in my library)'), 'ok');
      setSelId((old) => (old === paperId ? null : old));
      invalidate();
    },
    onError: (e) => toast(`${tr('移除失败：', 'Failed to remove: ')}${errText(e)}`, 'error'),
  });

  // 个人版 wiki 按需生成（wiki_source=none 的论文；费用记个人额度）
  const generateMutation = useMutation({
    mutationFn: (paperId: string) => api.compilePersonalWiki(paperId, pid),
    onSuccess: () => {
      toast(tr('个人版解读已生成', 'Personal wiki generated'), 'ok');
      invalidate();
    },
    onError: (e) => toast(`${tr('生成失败：', 'Failed to generate: ')}${errText(e)}`, 'error'),
  });

  const refreshSnapshotMutation = useMutation({
    mutationFn: (paperId: string) => api.refreshShelfSnapshot(pid, paperId),
    onSuccess: () => {
      toast(tr('快照已刷新', 'Snapshot refreshed'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['shelf', pid] });
    },
    onError: (e) => toast(`${tr('刷新失败：', 'Failed to refresh: ')}${errText(e)}`, 'error'),
  });

  // 多选导出：把勾选的 paper_id 子集导成 BibTeX（course 作用域，复用文献库同一端点）
  const exportMutation = useMutation({
    mutationFn: () => api.downloadCitations(pid, { format: 'bibtex', ids: [...checkedIds] }),
    onSuccess: (blob) => {
      saveBlob(blob, 'polaris-selected.bib');
      toast(tr(`已导出 ${checkedIds.size} 篇的 BibTeX`, `Exported BibTeX for ${checkedIds.size} papers`), 'ok');
    },
    onError: (e) => toast(`${tr('导出失败：', 'Export failed: ')}${errText(e)}`, 'error'),
  });

  const toggleCheck = (paperId: string) =>
    setCheckedIds((old) => {
      const next = new Set(old);
      if (next.has(paperId)) next.delete(paperId);
      else next.add(paperId);
      return next;
    });

  const countText = semantic
    ? semQuery.data === undefined
      ? ''
      : tr(`语义命中 ${semItems.length} 篇`, `${semItems.length} semantic matches`)
    : data === undefined
      ? ''
      : filter === 'all'
        ? tr(`共 ${data.total} 篇相关研究`, `${data.total} related papers`)
        : tr(`筛出 ${visible.length} 篇 · 共 ${data.total} 篇`, `${visible.length} shown · ${data.total} total`);

  // 语义态置灰高级检索 / 排序 / 状态过滤（这些只作用于关键词书架查询）
  const filterDisabled = semantic ? { opacity: 0.45, pointerEvents: 'none' as const } : undefined;

  return (
    <div
      className="page fadeup"
      style={{ maxWidth: 1360, display: 'flex', flexDirection: 'column', height: '100%', paddingBottom: 24 }}
    >
      <PageHead
        eyebrow="Polaris · Related Work"
        title={tr('相关研究', 'Related Work')}
        right={
          tab === 'list' ? (
            <button className="btn btn-primary sm" onClick={() => setAddOpen(true)}>
              <Icon name="plus" size={13} />
              {tr('添加论文', 'Add papers')}
            </button>
          ) : undefined
        }
      />

      {/* —— 页面级 tab：书架列表 / 相关研究对话 —— */}
      <div className="row" style={{ marginBottom: 14 }}>
        <Segmented<PageTab>
          options={[
            { v: 'list', label: tr('相关研究', 'Related work') },
            { v: 'chat', label: tr('文献对话', 'Chat') },
          ]}
          value={tab}
          onChange={setTab}
        />
      </div>

      {/* 关联文献库栏（语料来源）：仅列表视图显示 */}
      {tab === 'list' && (
        <LinkedLibrariesBar
          pid={pid}
          libs={libs}
          corpusTotal={corpusTotal}
          loading={sourceLibrariesQuery.isLoading}
          onNavigate={(path) => navigate(path)}
        />
      )}

      {/* —— 卡片容器（列表用双栏；对话直接铺满） —— */}

      <div
        className="card"
        style={{ overflow: 'hidden', display: 'flex', flexDirection: 'column', flex: 1, minHeight: 480 }}
      >
        {tab === 'chat' ? (
          <ShelfChatTab pid={pid} />
        ) : (
        <div className="split split-stackable">
          {/* —— 左：书架列表 —— */}
          <div className="split-list">
            {/* 工具栏：搜索 + 高级检索 + 排序 + 状态过滤 + 计数 */}
            <div style={{ padding: '12px 14px 10px', borderBottom: '0.5px solid var(--border)' }}>
              <div className="row gap8">
                <SearchInput
                  value={qInput}
                  onChange={setQInput}
                  placeholder={
                    scope === 'semantic'
                      ? tr('语义检索（自然语言描述）…', 'Semantic search (natural language)…')
                      : tr('搜索标题 / 作者…', 'Search title / authors…')
                  }
                />
                <AdvancedToggle
                  open={advOpen}
                  active={advActive}
                  onToggle={() => setAdvOpen((o) => !o)}
                  title={tr(
                    '高级检索：作者 / 机构 / 年份 / 阅读状态',
                    'Advanced search: author / affiliation / year / reading status',
                  )}
                />
              </div>

              {/* 关键词 / 语义检索切换（对齐文献库 LibraryBrowse） */}
              <div className="row gap6 wrap" style={{ marginTop: 8 }}>
                <span className={`chip${scope === 'keyword' ? ' on' : ''}`} onClick={() => setScope('keyword')}>
                  {tr('关键词', 'Keyword')}
                </span>
                <span
                  className={`chip${scope === 'semantic' ? ' on' : ''}`}
                  title={tr('用自然语言在课题语料里语义召回', 'Semantic recall over the topic corpus')}
                  onClick={() => setScope('semantic')}
                >
                  {tr('语义检索', 'Semantic')}
                </span>
              </div>

              {advOpen && (
                <div style={filterDisabled}>
                <AdvancedPanel onClear={advActive ? clearAdvanced : undefined}>
                  <div className="row gap8">
                    <FilterInput
                      value={author}
                      onChange={setAuthor}
                      placeholder={tr('作者姓名…', 'Author name…')}
                    />
                    <FilterInput
                      value={affiliation}
                      onChange={setAffiliation}
                      placeholder={tr('发表机构…', 'Affiliation…')}
                      title={tr('需要论文元数据带有机构信息', 'Needs affiliation metadata')}
                    />
                  </div>
                  <YearRangeField
                    label={tr('年份', 'Year')}
                    from={yearFrom}
                    to={yearTo}
                    onFrom={setYearFrom}
                    onTo={setYearTo}
                  />
                  <div className="row gap6 wrap" style={{ alignItems: 'center' }}>
                    <span style={{ width: 52, flexShrink: 0, fontSize: 11, color: 'var(--text-3)' }}>
                      {tr('阅读状态', 'Reading')}
                    </span>
                    {READING_FILTERS.map((f) => (
                      <span
                        key={f.v || 'all'}
                        className={`chip${readingStatus === f.v ? ' on' : ''}`}
                        onClick={() => setReadingStatus(f.v)}
                      >
                        {tr(f.zh, f.en)}
                      </span>
                    ))}
                  </div>
                  <label
                    className="row gap6"
                    style={{ fontSize: 11.5, color: 'var(--text-2)', cursor: 'pointer', alignItems: 'center' }}
                  >
                    <input type="checkbox" checked={starred} onChange={(e) => setStarred(e.target.checked)} />
                    {tr('只看星标', 'Starred only')}
                  </label>
                </AdvancedPanel>
                </div>
              )}

              <div className="row gap8" style={{ marginTop: 10, ...filterDisabled }}>
                <SelectMenu
                  value={sort}
                  options={SORTS.map((s) => ({ value: s.v, label: tr(s.zh, s.en) }))}
                  onChange={(v) => setSort(v as ShelfSort)}
                  wrapStyle={{ width: 132, flexShrink: 0 }}
                  style={{ height: 30, fontSize: 12 }}
                />
                <SelectMenu
                  value={filter}
                  options={FILTERS.map((f) => ({ value: f.v, label: tr(f.zh, f.en) }))}
                  onChange={(v) => setFilter(v as ShelfFilter)}
                  wrapStyle={{ marginLeft: 'auto', width: 118, flexShrink: 0 }}
                  style={{ height: 30, fontSize: 12 }}
                />
              </div>
              <div className="mono" style={{ marginTop: 8, fontSize: 10.5, color: 'var(--text-3)' }}>
                {countText}
              </div>
              {semFallback && (
                <div
                  style={{
                    marginTop: 8,
                    fontSize: 11,
                    color: 'var(--warn-tx)',
                    background: 'var(--warn-bg)',
                    borderRadius: 7,
                    padding: '5px 9px',
                    lineHeight: 1.5,
                  }}
                >
                  {tr('语义检索暂不可用，已回退为关键词匹配。', 'Semantic search unavailable — fell back to keyword matching.')}
                </div>
              )}
            </div>

            {/* 列表（自滚动） */}
            <div className="scroll" style={{ overflowY: 'auto', flex: 1 }}>
              {semantic ? (
                /* —— 语义检索结果 —— */
                semQuery.isLoading ? (
                  <div style={{ padding: 14 }} className="col gap12">
                    <div className="skel" style={{ height: 84 }} />
                    <div className="skel" style={{ height: 84 }} />
                    <div className="skel" style={{ height: 84 }} />
                  </div>
                ) : semQuery.isError ? (
                  <EmptyState
                    compact
                    icon="x"
                    title={tr('语义检索失败', 'Semantic search failed')}
                    desc={tr('后端暂时不可用，稍后再试或改用关键词。', 'Backend unavailable — retry later or switch to keyword.')}
                    action={
                      <button className="btn btn-soft sm" onClick={() => setScope('keyword')}>
                        {tr('改用关键词', 'Use keyword')}
                      </button>
                    }
                  />
                ) : semItems.length === 0 ? (
                  <EmptyState
                    compact
                    icon="search"
                    title={tr('没有语义匹配的论文', 'No semantic matches')}
                    desc={tr('换个说法，或改用关键词搜索。', 'Rephrase the query, or switch to keyword search.')}
                  />
                ) : (
                  semItems.map((item) => (
                    <ShelfRow
                      key={item.paper_id}
                      item={item}
                      active={item.paper_id === selected?.paper_id}
                      checked={checkedIds.has(item.paper_id)}
                      selectMode={selectMode}
                      onSelect={() => setSelId(item.paper_id)}
                      onToggleCheck={() => toggleCheck(item.paper_id)}
                    />
                  ))
                )
              ) : shelfQuery.isLoading ? (
                <div style={{ padding: 14 }} className="col gap12">
                  <div className="skel" style={{ height: 84 }} />
                  <div className="skel" style={{ height: 84 }} />
                  <div className="skel" style={{ height: 84 }} />
                </div>
              ) : shelfQuery.isError ? (
                <EmptyState
                  compact
                  icon="x"
                  title={tr('加载不出相关研究', 'Cannot load related work')}
                  desc={tr('后端暂时不可用，稍后再试。', 'The backend is unavailable — try again later.')}
                  action={
                    <button className="btn btn-soft sm" onClick={() => void shelfQuery.refetch()}>
                      {tr('重试', 'Retry')}
                    </button>
                  }
                />
              ) : items.length === 0 ? (
                hasServerFilter ? (
                  <EmptyState
                    compact
                    icon="search"
                    title={tr('没有匹配的论文', 'No matching papers')}
                    desc={tr('换个关键词或放宽高级检索条件。', 'Try another keyword or loosen the filters.')}
                    action={
                      <button
                        className="btn btn-soft sm"
                        onClick={() => {
                          setQInput('');
                          clearAdvanced();
                        }}
                      >
                        {tr('清除筛选', 'Clear filters')}
                      </button>
                    }
                  />
                ) : (
                  <EmptyState
                    compact
                    icon="pin"
                    title={tr('还没有添加论文', 'No papers yet')}
                    desc={tr('这个课题直接依赖的论文会列在这里。', 'Papers this topic builds on will show up here.')}
                  />
                )
              ) : visible.length === 0 ? (
                <EmptyState
                  compact
                  icon="search"
                  title={tr('没有这个状态的论文', 'No papers in this status')}
                  action={
                    <button className="btn btn-soft sm" onClick={() => setFilter('all')}>
                      {tr('清除过滤', 'Clear filter')}
                    </button>
                  }
                />
              ) : (
                visible.map((item) => (
                  <ShelfRow
                    key={item.paper_id}
                    item={item}
                    active={item.paper_id === selected?.paper_id}
                    checked={checkedIds.has(item.paper_id)}
                    selectMode={selectMode}
                    onSelect={() => setSelId(item.paper_id)}
                    onToggleCheck={() => toggleCheck(item.paper_id)}
                  />
                ))
              )}
            </div>

            {/* —— 底部操作栏：多选 + 导出 BibTeX（常驻，对齐文献库 PapersTab） —— */}
            <div
              className="row gap8"
              style={{ padding: '9px 14px', borderTop: '0.5px solid var(--border)', flexShrink: 0 }}
            >
              <button
                className={'btn sm ' + (selectMode ? 'btn-primary' : 'btn-ghost')}
                title={tr('开启后列表出现复选框，可批量导出引用', 'Show checkboxes to bulk-export citations')}
                onClick={() => {
                  setSelectMode((m) => !m);
                  setCheckedIds(new Set());
                }}
              >
                <Icon name="check" size={13} />
                {selectMode ? tr(`已选 ${checkedIds.size}`, `${checkedIds.size} selected`) : tr('多选', 'Select')}
              </button>
              {selectMode && (
                <button
                  className="btn btn-ghost sm"
                  disabled={checkedIds.size === 0 || exportMutation.isPending}
                  onClick={() => exportMutation.mutate()}
                >
                  {exportMutation.isPending ? (
                    <Icon name="refresh" size={12} style={{ animation: 'spin 1s linear infinite' }} />
                  ) : (
                    <Icon name="download" size={12} />
                  )}
                  {tr('导出 BibTeX', 'Export BibTeX')}
                </button>
              )}
            </div>

            {/* 底部分页栏（超过单页上限 100 才出现；语义结果不分页） */}
            {!semantic && totalPages > 1 && (
              <div
                className="row gap12"
                style={{
                  padding: '9px 14px',
                  borderTop: '0.5px solid var(--border)',
                  justifyContent: 'center',
                  flexShrink: 0,
                }}
              >
                <button className="btn btn-ghost sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
                  <Icon name="chevron" size={12} style={{ transform: 'rotate(180deg)' }} />
                  {tr('上一页', 'Prev')}
                </button>
                <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>
                  {tr(`第 ${page} / ${totalPages} 页`, `Page ${page} / ${totalPages}`)}
                </span>
                <button
                  className="btn btn-ghost sm"
                  disabled={page >= totalPages}
                  onClick={() => setPage((p) => p + 1)}
                >
                  {tr('下一页', 'Next')}
                  <Icon name="chevron" size={12} />
                </button>
              </div>
            )}
          </div>

          {/* —— 右：详情 / 空态引导 —— */}
          <div className="split-detail">
            {selected ? (
              <ShelfDetailPane
                key={selected.paper_id}
                item={selected}
                notePending={noteMutation.isPending && noteMutation.variables?.paperId === selected.paper_id}
                onSaveNote={(note) => noteMutation.mutate({ paperId: selected.paper_id, note })}
                removePending={removeMutation.isPending}
                onRemove={() => removeMutation.mutate(selected.paper_id)}
                generating={generateMutation.isPending && generateMutation.variables === selected.paper_id}
                onGenerateWiki={() => generateMutation.mutate(selected.paper_id)}
                refreshing={
                  refreshSnapshotMutation.isPending && refreshSnapshotMutation.variables === selected.paper_id
                }
                onRefreshSnapshot={() => refreshSnapshotMutation.mutate(selected.paper_id)}
                onShelf={!semantic || selectedOnShelf}
                onAdd={() => addMutation.mutate(selected.paper_id)}
                addPending={addMutation.isPending && addMutation.variables === selected.paper_id}
              />
            ) : !semantic && shelfQuery.isSuccess && items.length === 0 && !hasServerFilter ? (
              /* 书架为空 → 右栏放引导 */
              <div style={{ margin: 'auto' }}>
                <EmptyState
                  icon="pin"
                  title={tr('从文献库挑几篇开始', 'Start by picking a few papers')}
                  desc={tr(
                    '把这个课题直接依赖的论文放进来，给每篇写一句为什么相关——后面的想法、实验、写作都会以这里为地基。',
                    'Shelve the papers this topic builds on and note why each matters — ideas, experiments and writing all build on this.',
                  )}
                  action={
                    <div className="row gap10" style={{ justifyContent: 'center' }}>
                      <button className="btn btn-primary sm" onClick={() => setAddOpen(true)}>
                        <Icon name="plus" size={13} />
                        {tr('添加论文', 'Add papers')}
                      </button>
                      <button className="btn btn-soft sm" onClick={() => navigate(libEntryHref)}>
                        <Icon name="book" size={13} />
                        {libEntryLabel}
                      </button>
                    </div>
                  }
                />
              </div>
            ) : (
              <div className="empty" style={{ margin: 'auto' }}>
                {tr('选择论文查看详情', 'Select a paper to view details')}
              </div>
            )}
          </div>
        </div>
        )}
      </div>

      {/* —— 添加论文（从文献库 / 手动）统一弹窗 —— */}
      <AddPaperModal
        open={addOpen}
        onClose={() => setAddOpen(false)}
        pid={pid}
        shelvedIds={shelvedIds}
        libraryHref={libEntryHref}
        libraryLabel={libEntryLabel}
        addPending={addMutation.isPending}
        onAdd={(paperId) => addMutation.mutate(paperId)}
        importPending={importMutation.isPending}
        onImport={(input) => importMutation.mutateAsync(input)}
      />

      {progress && (
        <PaperProgressModal
          taskId={progress.taskId}
          paperTitle={progress.title}
          onClose={() => setProgress(null)}
          onDone={invalidate}
        />
      )}
    </div>
  );
}
