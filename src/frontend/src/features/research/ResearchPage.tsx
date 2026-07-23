import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { Segmented } from '../../components/ui/Segmented';
import { SelectMenu } from '../../components/ui/SelectMenu';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { api, type ShelfImportInput, type ShelfItemRead, type ShelfWikiSource } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { topicPath, useProject } from '../../app/project';
import { libraryPath, useTopicLibrary } from '../libraries/hooks';
import { AddPaperModal } from './AddPaperModal';
import { ShelfDetailPane, WikiBadge } from './ShelfDetailPane';

/* ============================================================
   /t/:topicId/research — 课题「相关研究」书架。
   双栏主从布局对齐「我的文献库」：左栏紧凑论文行（排序 / 状态
   过滤 / 计数），右栏选中论文的完整详情（wiki 渲染、课题备注、
   就地动作）；添加统一收进「添加论文」弹窗（从文献库 / 手动）。
   入架同时自动收藏进「我的文献库」；移出书架不动个人库。
   ============================================================ */

// 后端单页上限 100；书架通常远小于此，客户端排序/过滤在页内完成
const PAGE_SIZE = 100;

type ShelfSort = 'added' | 'year';
type ShelfFilter = 'all' | ShelfWikiSource;

// 模块级常量不调 tr()：保留 zh/en 字段，渲染处再 tr
const SORTS: { v: ShelfSort; zh: string; en: string }[] = [
  { v: 'added', zh: '按添加时间', en: 'By added' },
  { v: 'year', zh: '按年份', en: 'By year' },
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
  onSelect,
}: {
  item: ShelfItemRead;
  active: boolean;
  onSelect: () => void;
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
      {/* 顶部 mono 元信息行：编号/venue + 年份；右侧解读状态徽标 */}
      <div className="row gap8" style={{ marginBottom: 5 }}>
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

/* ---------------- 页面 ---------------- */

export function ResearchPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { currentProjectId } = useProject();
  const pid = currentProjectId ?? '';
  // 文献库入口：课题隐式库详情页（列表未就绪时退回旧 /wiki 路径由重定向兜底）
  const topicLib = useTopicLibrary(pid || null);
  const wikiHref = topicLib ? libraryPath(topicLib.id) : topicPath(pid, 'wiki');

  const [page, setPage] = useState(1);
  const [sort, setSort] = useState<ShelfSort>('added');
  const [filter, setFilter] = useState<ShelfFilter>('all');
  const [selId, setSelId] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);

  useEffect(() => {
    setPage(1);
    setSelId(null);
    setFilter('all');
  }, [pid]);

  const shelfQuery = useQuery({
    queryKey: ['shelf', pid, page],
    queryFn: () => api.listShelf(pid, { page, size: PAGE_SIZE }),
    enabled: !!pid,
    retry: false,
    placeholderData: keepPreviousData,
  });
  const idsQuery = useQuery({
    queryKey: ['shelf-ids', pid],
    queryFn: () => api.listShelfIds(pid),
    enabled: !!pid,
    retry: false,
  });
  const shelvedIds = new Set(idsQuery.data?.paper_ids ?? []);

  const data = shelfQuery.data;
  const items = useMemo(() => data?.items ?? [], [data]);
  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.size)) : 1;

  // 客户端排序 + 状态过滤（书架规模小，页内完成）
  const visible = useMemo(() => {
    const filtered = filter === 'all' ? items : items.filter((i) => i.wiki_source === filter);
    const sorted = [...filtered];
    if (sort === 'year') sorted.sort((a, b) => (b.year ?? -1) - (a.year ?? -1));
    else sorted.sort((a, b) => b.added_at.localeCompare(a.added_at));
    return sorted;
  }, [items, filter, sort]);

  // 选中项：优先手动选择；不在可见列表（被过滤/移出）时退回第一条
  const selected = visible.find((i) => i.paper_id === selId) ?? visible[0] ?? null;

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
      toast(tr('已添加到相关研究', 'Added to related work'), 'ok');
      setSelId(item.paper_id);
      invalidate();
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

  const countText =
    data === undefined
      ? ''
      : filter === 'all'
        ? tr(`共 ${data.total} 篇相关研究`, `${data.total} related papers`)
        : tr(`筛出 ${visible.length} 篇 · 共 ${data.total} 篇`, `${visible.length} shown · ${data.total} total`);

  return (
    <div
      className="page fadeup"
      style={{ maxWidth: 1360, display: 'flex', flexDirection: 'column', height: '100%', paddingBottom: 24 }}
    >
      <PageHead
        eyebrow="Polaris · Related Work"
        title={tr('相关研究', 'Related Work')}
        sub={tr(
          '这个课题直接依赖的论文：从文献库挑选，写下为什么相关，随手翻解读。',
          'Papers this topic builds on: pick from the library, note why they matter, read the wikis.',
        )}
        right={
          <button className="btn btn-primary sm" onClick={() => setAddOpen(true)}>
            <Icon name="plus" size={13} />
            {tr('添加论文', 'Add papers')}
          </button>
        }
      />

      {/* —— 双栏卡片容器（同「我的文献库」外壳；窄屏上下堆叠） —— */}
      <div
        className="card"
        style={{ overflow: 'hidden', display: 'flex', flexDirection: 'column', flex: 1, minHeight: 480 }}
      >
        <div className="split split-stackable">
          {/* —— 左：书架列表 —— */}
          <div className="split-list">
            {/* 工具栏：排序 + 状态过滤 + 计数 */}
            <div style={{ padding: '12px 14px 10px', borderBottom: '0.5px solid var(--border)' }}>
              <div className="row gap8">
                <Segmented<ShelfSort>
                  options={SORTS.map((s) => ({ v: s.v, label: tr(s.zh, s.en) }))}
                  value={sort}
                  onChange={setSort}
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
            </div>

            {/* 列表（自滚动） */}
            <div className="scroll" style={{ overflowY: 'auto', flex: 1 }}>
              {shelfQuery.isLoading ? (
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
                <EmptyState
                  compact
                  icon="pin"
                  title={tr('还没有添加论文', 'No papers yet')}
                  desc={tr('这个课题直接依赖的论文会列在这里。', 'Papers this topic builds on will show up here.')}
                />
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
                    onSelect={() => setSelId(item.paper_id)}
                  />
                ))
              )}
            </div>

            {/* 底部分页栏（超过单页上限 100 才出现） */}
            {totalPages > 1 && (
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
              />
            ) : shelfQuery.isSuccess && items.length === 0 ? (
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
                      <button className="btn btn-soft sm" onClick={() => navigate(wikiHref)}>
                        <Icon name="book" size={13} />
                        {tr('去文献库', 'Open the library')}
                      </button>
                    </div>
                  }
                />
              </div>
            ) : (
              <div className="empty" style={{ margin: 'auto' }}>
                {tr('选择左侧论文查看详情', 'Select a paper to view details')}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* —— 添加论文（从文献库 / 手动）统一弹窗 —— */}
      <AddPaperModal
        open={addOpen}
        onClose={() => setAddOpen(false)}
        pid={pid}
        shelvedIds={shelvedIds}
        wikiHref={wikiHref}
        addPending={addMutation.isPending}
        onAdd={(paperId) => addMutation.mutate(paperId)}
        importPending={importMutation.isPending}
        onImport={(input) => importMutation.mutateAsync(input)}
      />
    </div>
  );
}
