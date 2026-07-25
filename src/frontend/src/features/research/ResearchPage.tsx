import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { SelectMenu } from '../../components/ui/SelectMenu';
import { Segmented } from '../../components/ui/Segmented';
import { EmptyState } from '../../components/ui/EmptyState';
import { citationExportItems, ExportDropdown } from '../../components/ui/ExportDropdown';
import { toast } from '../../components/ui/Toast';
import {
  api,
  type CitationFormat,
  type DirectionLibrarySummary,
  type PaperRead,
  type ReadingStatus,
  type ShelfImportInput,
  type ShelfItemRead,
  type ShelfSort,
  type ShelfWikiSource,
} from '../../lib/api';
import { fmtRelative } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { useProject } from '../../app/project';
import { libraryPath } from '../libraries/hooks';
import { TrashModal, type TrashItemView } from '../shared/TrashModal';
import {
  AdvancedPanel,
  AdvancedToggle,
  FilterInput,
  MyTagField,
  parseYear,
  ReadingStatusField,
  saveBlob,
  SearchInput,
  SemanticSwitch,
  useDebounced,
  YearRangeField,
} from '../wiki/shared';
import { AddPaperModal } from './AddPaperModal';
import { PaperProgressModal } from '../library/PaperProgressModal';
import { ShelfChatTab } from './ShelfChatTab';
import { ShelfDetailPane, WikiBadge } from './ShelfDetailPane';

/* ============================================================
   /t/:topicId/research — 课题相关研究书架。
   双栏主从布局对齐我的文献库：左栏紧凑论文行（排序 / 状态
   过滤 / 计数），右栏选中论文的完整详情（wiki 渲染、课题备注、
   就地动作）；添加统一收进添加论文弹窗（从文献库 / 手动）。
   入架同时自动收藏进我的文献库；移出书架不动个人库。
   ============================================================ */

// 后端单页上限 100；排序/关键词/筛选走后端，wiki_source 状态过滤在页内完成
const PAGE_SIZE = 100;

type ShelfFilter = 'all' | ShelfWikiSource;
/** 页面级 tab：书架列表 / 相关研究对话 */
type PageTab = 'list' | 'chat';
/** 阅读状态筛选：空串=不限；其余透传给后端 reading_status。 */
type ReadingFilter = '' | ReadingStatus;

/** 语义检索命中的 ScoredPaper（课题语料，未必已入书架）映射成书架行需要的最小字段。
    note / wiki_content / snapshot_at / source_library_id 语义结果里没有，按缺省填；
    wiki_source 用 has_wiki 粗略推断（有解读→库版徽标，没有→暂无）。行/详情只作展示用，
    真正的备注/移出/生成走 (pid, paper_id) 幂等接口，不依赖这些映射字段。 */
function scoredToShelf(p: PaperRead & { score?: number | null }): ShelfItemRead {
  return {
    paper_id: p.id,
    title: p.title,
    authors: p.authors,
    affiliations: p.affiliations,
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
          title={tr('选中后可批量移出 / 导出引用', 'Select for bulk remove / citation export')}
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

/* ---------------- 关联文献库一栏（列表下方） ---------------- */

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

/** 列表下方的一栏：课题关联的文献库，点库名进对应的库详情页。 */
function LinkedLibrariesRow({
  pid,
  libs,
  loading,
  onNavigate,
}: {
  pid: string;
  libs: DirectionLibrarySummary[];
  loading: boolean;
  onNavigate: (path: string) => void;
}) {
  if (loading) {
    return (
      <div className="row gap8" style={{ marginTop: 12, flexShrink: 0 }}>
        <div className="skel" style={{ height: 22, width: 160 }} />
        <div className="skel" style={{ height: 22, width: 120 }} />
      </div>
    );
  }

  return (
    <div
      className="row gap8"
      style={{ marginTop: 12, flexShrink: 0, flexWrap: 'wrap', alignItems: 'center', fontSize: 12.5 }}
    >
      <span className="row gap6" style={{ color: 'var(--text-3)', flexShrink: 0 }}>
        <Icon name="book" size={13} style={{ color: 'var(--accent)' }} />
        {tr('关联文献库', 'Linked libraries')}
      </span>
      {libs.length === 0 ? (
        <>
          <span style={{ color: 'var(--text-3)' }}>
            {tr('还没关联，先关联一个再挑论文。', 'None yet — link one to pick papers from.')}
          </span>
          <button className="btn btn-ghost sm" onClick={() => onNavigate(`/projects/${pid}`)}>
            <Icon name="link" size={12} />
            {tr('去关联', 'Link a library')}
          </button>
        </>
      ) : (
        libs.map((lib) => (
          <button
            key={lib.id}
            className="btn btn-ghost sm"
            style={{ maxWidth: 300 }}
            title={lib.name}
            onClick={() => onNavigate(libraryPath(lib.id))}
          >
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>
              {lib.name}
            </span>
            <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)', flexShrink: 0 }}>
              {tr(`${lib.paper_count} 篇`, `${lib.paper_count}`)}
            </span>
            <LibStatusBadge status={lib.status} />
          </button>
        ))
      )}
    </div>
  );
}

/* ---------------- 回收站 ---------------- */

/** 相关研究回收站：弹窗外壳复用共享 TrashModal，端点与行映射留在这里。
    移出书架是软删，条目落在这里，可召回或彻底删除；个人库收藏全程不动。 */
function ShelfTrashModal({ pid, open, onClose }: { pid: string; open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();

  const trashQuery = useQuery({
    queryKey: ['shelf-trash', pid],
    queryFn: () => api.listShelf(pid, { trashed: true, size: PAGE_SIZE }),
    enabled: open && !!pid,
    retry: false,
  });
  const trashed = useMemo(() => trashQuery.data?.items ?? [], [trashQuery.data]);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['shelf-trash', pid] });
    void queryClient.invalidateQueries({ queryKey: ['shelf', pid] });
    void queryClient.invalidateQueries({ queryKey: ['shelf-ids', pid] });
  };

  const restoreMutation = useMutation({
    mutationFn: (paperId: string) => api.restoreShelfItem(pid, paperId),
    onSuccess: (item) => {
      toast(`${tr('已召回：', 'Restored: ')}${item.title.slice(0, 30)}`, 'ok');
      invalidate();
    },
    onError: (e) => toast(`${tr('召回失败：', 'Restore failed: ')}${errText(e)}`, 'error'),
  });

  const purgeMutation = useMutation({
    mutationFn: (paperId: string) => api.removeFromShelf(pid, paperId, { hard: true }),
    onSuccess: () => {
      toast(tr('已彻底删除', 'Permanently deleted'), 'ok');
      invalidate();
    },
    onError: (e) => toast(`${tr('删除失败：', 'Delete failed: ')}${errText(e)}`, 'error'),
  });

  const emptyMutation = useMutation({
    mutationFn: () => api.emptyShelfTrash(pid),
    onSuccess: (res) => {
      toast(tr(`回收站已清空（${res.deleted} 篇）`, `Trash emptied (${res.deleted} papers)`), 'ok');
      invalidate();
    },
    onError: (e) => toast(`${tr('清空失败：', 'Empty failed: ')}${errText(e)}`, 'error'),
  });

  const items = useMemo<TrashItemView[]>(
    () =>
      trashed.map((item) => {
        const authors = item.authors.map((a) => a.name).join(', ');
        return {
          id: item.paper_id,
          code: item.arxiv_id ?? item.venue ?? '—',
          year: item.year,
          title: item.title,
          aside: (
            <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>
              {tr(`${fmtRelative(item.trashed_at)} 移入`, `trashed ${fmtRelative(item.trashed_at)}`)}
            </span>
          ),
          desc: item.tldr ?? (authors || null),
          searchText: [item.title, authors].join('\n'),
        };
      }),
    [trashed],
  );

  return (
    <TrashModal
      open={open}
      onClose={onClose}
      sub={tr(
        '个人库里的收藏不受影响',
        'Your saved copies in my library are untouched',
      )}
      items={items}
      total={trashQuery.data?.total}
      loading={trashQuery.isLoading}
      busy={restoreMutation.isPending || purgeMutation.isPending || emptyMutation.isPending}
      emptying={emptyMutation.isPending}
      onRestore={(id) => restoreMutation.mutate(id)}
      onPurge={(id) => purgeMutation.mutate(id)}
      onEmpty={() => emptyMutation.mutate()}
      emptyWarning={(n) =>
        tr(
          `将彻底删除回收站里的全部 ${n} 篇，无法恢复（个人库收藏不受影响）`,
          `This permanently deletes all ${n} papers in the trash — no undo (your saved copies stay)`,
        )
      }
      restoreHint={tr('召回到相关研究', 'Restore to related work')}
      purgeHint={tr('彻底删除，无法再召回（个人库收藏不受影响）', 'Delete forever — cannot be restored (saved copies stay)')}
    />
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
  const [trashOpen, setTrashOpen] = useState(false);
  // 个人补充入架后若后端返回 task_id，弹出分阶段处理进度
  const [progress, setProgress] = useState<{ taskId: string; title: string } | null>(null);

  // 关键词 + 高级检索条件（走后端）
  const [qInput, setQInput] = useState('');
  const q = useDebounced(qInput.trim());
  // 语义检索开关（true=课题语料向量召回，false=关键词书架过滤）
  const [semanticOn, setSemanticOn] = useState(false);
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
  const [myTag, setMyTag] = useState('');

  const advActive =
    !!author.trim() ||
    !!affiliation.trim() ||
    !!yearFrom.trim() ||
    !!yearTo.trim() ||
    readingStatus !== '' ||
    starred ||
    !!myTag;
  // 是否有任何后端筛选（用于空态文案区分「没添加」vs「没匹配」）
  const hasServerFilter = !!q || advActive;

  const clearAdvanced = () => {
    setAuthor('');
    setAffiliation('');
    setYearFrom('');
    setYearTo('');
    setReadingStatus('');
    setStarred(false);
    setMyTag('');
  };

  // 点作者/机构 → 列表只留匹配的论文（走已有的高级检索），其余条件重置并展开面板
  const applyAdvFilter = useCallback((patch: { author?: string; affiliation?: string }) => {
    setSemanticOn(false);
    setQInput('');
    setAuthor(patch.author ?? '');
    setAffiliation(patch.affiliation ?? '');
    setYearFrom('');
    setYearTo('');
    setReadingStatus('');
    setStarred(false);
    setMyTag('');
    setAdvOpen(true);
    setSelId(null);
    if (patch.author) {
      toast(tr(`已筛选作者：${patch.author}`, `Filtered by author: ${patch.author}`), 'info');
    } else if (patch.affiliation) {
      toast(tr(`已筛选机构：${patch.affiliation}`, `Filtered by affiliation: ${patch.affiliation}`), 'info');
    }
  }, []);
  const filterByAuthor = useCallback((name: string) => applyAdvFilter({ author: name }), [applyAdvFilter]);
  const filterByAffiliation = useCallback(
    (name: string) => applyAdvFilter({ affiliation: name }),
    [applyAdvFilter],
  );

  useEffect(() => {
    setPage(1);
    setSelId(null);
    setFilter('all');
    setQInput('');
    setSemanticOn(false);
    clearAdvanced();
  }, [pid]);

  // 后端筛选/排序变化时回到第一页
  useEffect(() => {
    setPage(1);
  }, [q, sort, author, affiliation, yearFrom, yearTo, readingStatus, starred, myTag]);

  // 切换课题 / 搜索词 / 过滤 / 作用域时退出多选（对齐文献库 PapersTab）
  useEffect(() => {
    setCheckedIds(new Set());
    setSelectMode(false);
  }, [pid, q, semanticOn, filter, sort]);

  // 语义检索：有查询词且开关打开时激活；结果替换列表、不分页、置灰其余过滤
  const semantic = !!q && semanticOn;

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
      myTag,
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
        my_tag: myTag || undefined,
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
  const libs = useMemo<DirectionLibrarySummary[]>(() => sourceLibrariesQuery.data ?? [], [sourceLibrariesQuery.data]);

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

  // 移出 = 软删：条目进回收站，可召回；个人库收藏不动
  const removeMutation = useMutation({
    mutationFn: (paperId: string) => api.removeFromShelf(pid, paperId),
    onSuccess: (_d, paperId) => {
      toast(tr('已移入回收站，可以召回（个人库收藏保留）', 'Moved to trash — you can restore it (still saved in my library)'), 'ok');
      setSelId((old) => (old === paperId ? null : old));
      invalidate();
      void queryClient.invalidateQueries({ queryKey: ['shelf-trash', pid] });
    },
    onError: (e) => toast(`${tr('移除失败：', 'Failed to remove: ')}${errText(e)}`, 'error'),
  });

  // 多选批量移出：后端没有批量端点，按选中集逐篇调（同样是软删，落回收站）
  const bulkRemoveMutation = useMutation({
    mutationFn: async (paperIds: string[]) => {
      await Promise.all(paperIds.map((paperId) => api.removeFromShelf(pid, paperId)));
      return paperIds.length;
    },
    onSuccess: (n, paperIds) => {
      toast(
        tr(
          `已把 ${n} 篇移入回收站，可以召回（个人库收藏保留）`,
          `Moved ${n} papers to trash — restorable (saved copies stay)`,
        ),
        'ok',
      );
      setSelId((old) => (old && paperIds.includes(old) ? null : old));
      setCheckedIds(new Set());
      setSelectMode(false);
      invalidate();
      void queryClient.invalidateQueries({ queryKey: ['shelf-trash', pid] });
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

  // 多选导出：把勾选的 paper_id 子集导出引用（course 作用域，复用文献库同一端点）
  const exportMutation = useMutation({
    mutationFn: (format: CitationFormat) => api.downloadCitations(pid, { format, ids: [...checkedIds] }),
    onSuccess: (blob, format) => {
      saveBlob(blob, format === 'bibtex' ? 'polaris-selected.bib' : 'polaris-selected.json');
      toast(tr(`已导出 ${checkedIds.size} 篇`, `Exported ${checkedIds.size} papers`), 'ok');
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
      className="page fadeup page-fill"
      style={{ maxWidth: 1360, paddingBottom: 24 }}
    >
      {/* —— 页面级 tab：书架列表 / 相关研究对话；操作并在同一行右侧 —— */}
      <div className="row page-tabs" style={{ marginBottom: 14 }}>
        <Segmented<PageTab>
          options={[
            { v: 'list', label: tr('相关研究', 'Related work') },
            { v: 'chat', label: tr('文献对话', 'Chat') },
          ]}
          value={tab}
          onChange={setTab}
        />
        {tab === 'list' && (
          <button
            className="btn btn-primary sm"
            style={{ marginLeft: 'auto' }}
            onClick={() => setAddOpen(true)}
          >
            <Icon name="plus" size={13} />
            {tr('添加文献', 'Add paper')}
          </button>
        )}
      </div>

      {/* —— 卡片容器（列表用双栏；对话直接铺满） —— */}

      <div
        className="card split-card"
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
                    semanticOn
                      ? tr('语义检索（自然语言描述）…', 'Semantic search (natural language)…')
                      : tr('搜索标题 / 作者…', 'Search title / authors…')
                  }
                />
                <SemanticSwitch
                  checked={semanticOn}
                  onChange={setSemanticOn}
                  title={tr('打开后用自然语言在课题语料里语义召回', 'Semantic recall over the topic corpus')}
                />
                <AdvancedToggle
                  open={advOpen}
                  active={advActive}
                  onToggle={() => setAdvOpen((o) => !o)}
                  title={tr(
                    '高级检索：作者 / 机构 / 年份 / 我的标签 / 阅读状态 / 星标',
                    'Advanced search: author / affiliation / year / my tags / reading status / starred',
                  )}
                />
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
                  <MyTagField value={myTag} onChange={setMyTag} />
                  <ReadingStatusField value={readingStatus} onChange={setReadingStatus} />
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
                      <button className="btn btn-soft sm" onClick={() => setSemanticOn(false)}>
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
            {/* —— 底部操作栏：多选（删除 / 导出）+ 回收站入口，与文献工作台 / 我的文献库同款 —— */}
            <div
              className="row gap8"
              style={{ padding: '9px 14px', borderTop: '0.5px solid var(--border)', flexShrink: 0 }}
            >
              <button
                className={'btn sm ' + (selectMode ? 'btn-primary' : 'btn-ghost')}
                title={tr('开启后列表出现复选框，可批量移出 / 导出引用', 'Show checkboxes for bulk remove / citation export')}
                onClick={() => {
                  setSelectMode((m) => !m);
                  setCheckedIds(new Set());
                }}
              >
                <Icon name="check" size={13} />
                {selectMode
                  ? tr(`已选 ${checkedIds.size} 篇`, `${checkedIds.size} selected`)
                  : tr('多选', 'Select')}
              </button>
              {selectMode && (
                <>
                  <button
                    className="btn btn-ghost sm"
                    style={{ color: 'var(--danger-tx)' }}
                    title={tr('移入回收站（可召回，个人库收藏保留）', 'Move to trash (restorable, saved copies stay)')}
                    disabled={checkedIds.size === 0 || bulkRemoveMutation.isPending}
                    onClick={() => bulkRemoveMutation.mutate([...checkedIds])}
                  >
                    <Icon name="x" size={12} />
                    {tr('删除', 'Delete')}
                  </button>
                  <ExportDropdown
                    sm
                    placement="up"
                    align="left"
                    busy={exportMutation.isPending}
                    disabled={checkedIds.size === 0}
                    items={citationExportItems((format) => exportMutation.mutate(format))}
                  />
                </>
              )}
              <button
                className="btn btn-ghost sm"
                style={{ marginLeft: 'auto' }}
                title={tr('回收站：移出的论文可以召回或彻底删除', 'Trash: removed papers — restore or delete forever')}
                onClick={() => setTrashOpen(true)}
              >
                <Icon name="trash" size={13} />
                {tr('回收站', 'Trash')}
              </button>
            </div>
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
                onFilterAuthor={filterByAuthor}
                onFilterAffiliation={filterByAffiliation}
              />
            ) : !semantic && shelfQuery.isSuccess && items.length === 0 && !hasServerFilter ? (
              /* 书架为空 → 右栏放引导 */
              <div style={{ margin: 'auto' }}>
                <EmptyState
                  icon="pin"
                  title={tr('从文献库挑几篇开始', 'Start by picking a few papers')}
                  desc={tr(
                    '把课题直接依赖的论文放进来，写一句为什么相关。',
                    'Shelve the papers this topic builds on, and note why each matters.',
                  )}
                  action={
                    <div className="row gap10" style={{ justifyContent: 'center' }}>
                      <button className="btn btn-primary sm" onClick={() => setAddOpen(true)}>
                        <Icon name="plus" size={13} />
                        {tr('添加文献', 'Add paper')}
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

      {/* 关联文献库：列表下方一栏，点库名进对应的库 */}
      {tab === 'list' && (
        <LinkedLibrariesRow
          pid={pid}
          libs={libs}
          loading={sourceLibrariesQuery.isLoading}
          onNavigate={(path) => navigate(path)}
        />
      )}

      {/* —— 添加文献（从文献库 / 手动）统一弹窗 —— */}
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

      {/* —— 回收站（移出的论文：召回 / 彻底删除 / 清空） —— */}
      <ShelfTrashModal pid={pid} open={trashOpen} onClose={() => setTrashOpen(false)} />

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
