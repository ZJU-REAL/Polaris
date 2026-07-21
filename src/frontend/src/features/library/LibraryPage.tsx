import { useEffect, useState } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { Segmented } from '../../components/ui/Segmented';
import { EmptyState } from '../../components/ui/EmptyState';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { toast } from '../../components/ui/Toast';
import { api, type LibraryEntry, type LibrarySort, type LibraryTab, type Publication } from '../../lib/api';
import { fmtRelative } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { SearchInput, useDebounced } from '../wiki/shared';
import { PublicationsTab, PUBLICATIONS_PAGE_SIZE } from './PublicationsTab';
import { AuthorBindWizard } from './AuthorBindWizard';
import { entrySnapshot, LibraryDetailPane, pubSnapshot } from './LibraryDetailPane';

/* ============================================================
   /library — 我的文献库：
   「我的收藏」+「浏览记录」+「我发表的」三个 tab；
   双栏主从布局对齐文献追踪（Stage 00）的论文库：
   左栏列表（搜索/排序/分页/行操作），右栏选中条目的详情
   （活体论文展示 wiki，快照条目展示元数据 + 外链）。
   「我发表的」未填署名信息时表单占满整卡，填好后同样双栏。
   ============================================================ */

const PAGE_SIZE = 20;

/** 页面级 tab：库内两个 tab + 「我发表的」。 */
type PageTab = LibraryTab | 'publications';

// 模块级常量不调 tr()：保留 zh/en 字段，渲染处再 tr
const SORTS: { v: LibrarySort; zh: string; en: string }[] = [
  { v: 'recent', zh: '按最近浏览', en: 'By recency' },
  { v: 'title', zh: '按标题', en: 'By title' },
  { v: 'visits', zh: '按次数', en: 'By visits' },
];

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/* ---------------- 列表行（同文献追踪 PaperRow 版式 + active 选中态） ---------------- */

function EntryRow({
  entry,
  tab,
  active,
  busy,
  onSelect,
  onToggleSave,
  onPurge,
}: {
  entry: LibraryEntry;
  tab: LibraryTab;
  active: boolean;
  busy: boolean;
  onSelect: () => void;
  onToggleSave: () => void;
  onPurge: () => void;
}) {
  const authors = entry.authors.map((a) => a.name).join(', ');
  const summary = entry.tldr ?? entry.abstract;
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
        display: 'flex',
        gap: 12,
        alignItems: 'flex-start',
        cursor: 'pointer',
        background: active ? 'var(--accent-soft)' : 'transparent',
        borderLeft: active ? '2px solid var(--accent)' : '2px solid transparent',
        transition: 'background 0.12s',
      }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        {/* 顶部 mono 元信息行：编号/venue + 年份 + 源方向状态；右侧浏览信息 */}
        <div className="row gap8" style={{ marginBottom: 5 }}>
          <span className="mono" style={{ fontSize: 10.5, color: active ? 'var(--accent-text)' : 'var(--text-3)' }}>
            {entry.arxiv_id ?? entry.venue ?? '—'}
          </span>
          {entry.year !== null && (
            <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>
              {entry.year}
            </span>
          )}
          {entry.last_paper_id === null && (
            <span style={{ fontSize: 10.5, color: 'var(--text-4)' }}>
              {tr('源方向已删除', 'Source direction deleted')}
            </span>
          )}
          {entry.visit_count > 0 && (
            <span className="mono" style={{ marginLeft: 'auto', fontSize: 10.5, color: 'var(--text-4)', flexShrink: 0 }}>
              {fmtRelative(entry.last_visited_at)}
              {' · '}
              {tr(`看过 ${entry.visit_count} 次`, `${entry.visit_count} visits`)}
            </span>
          )}
        </div>
        <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.35, color: 'var(--text)' }}>
          {entry.title}
        </div>
        {authors && (
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
          </div>
        )}
        {/* 底部行：venue（编号占位时）+ 摘要/tldr 截断 */}
        {(summary || (entry.venue && entry.arxiv_id)) && (
          <div className="row gap8" style={{ marginTop: 6 }}>
            {entry.venue && entry.arxiv_id && (
              <span style={{ fontSize: 11.5, color: 'var(--text-3)', flexShrink: 0 }}>{entry.venue}</span>
            )}
            {summary && (
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
                {summary}
              </span>
            )}
          </div>
        )}
      </div>

      {/* —— 右：操作 —— */}
      <div className="row gap6" style={{ flexShrink: 0 }} onClick={(e) => e.stopPropagation()}>
        <button
          className="icon-btn"
          disabled={busy}
          title={entry.saved ? tr('取消收藏', 'Unsave') : tr('收藏', 'Save')}
          style={{ color: entry.saved ? 'var(--accent)' : 'var(--text-3)' }}
          onClick={onToggleSave}
        >
          <Icon name={entry.saved ? 'bookmarkFill' : 'bookmark'} size={15} />
        </button>
        {tab === 'history' && (
          <button
            className="icon-btn"
            disabled={busy}
            title={tr('彻底删除这条记录', 'Delete this record')}
            style={{ color: 'var(--text-3)' }}
            onClick={onPurge}
          >
            <Icon name="trash" size={15} />
          </button>
        )}
      </div>
    </div>
  );
}

/** 右栏未选中时的轻量空态。 */
function PickHint() {
  return (
    <div className="empty" style={{ margin: 'auto' }}>
      {tr('选择左侧论文查看详情', 'Select a paper to view details')}
    </div>
  );
}

export function LibraryPage() {
  const queryClient = useQueryClient();

  const [tab, setTab] = useState<PageTab>('saved');
  const [qInput, setQInput] = useState('');
  const q = useDebounced(qInput.trim());
  const [sort, setSort] = useState<LibrarySort>('recent');
  const [page, setPage] = useState(1);
  const [clearOpen, setClearOpen] = useState(false);

  // 双栏选中态：收藏/记录共用一份（同一种条目），「我发表的」单独一份。
  // 翻页/搜索/切 tab 都不清空——右栏基于选中时的快照继续展示，
  // 当前页里若还有同 id 条目则换用最新数据。
  const [selEntry, setSelEntry] = useState<LibraryEntry | null>(null);
  const [selPub, setSelPub] = useState<Publication | null>(null);
  // 「我发表的」：修改署名信息时整卡显示表单
  const [editingAuthor, setEditingAuthor] = useState(false);

  // tab / 搜索词 / 排序变化时回到第一页
  useEffect(() => {
    setPage(1);
  }, [tab, q, sort]);

  const onLibraryTab = tab !== 'publications';
  const listQuery = useQuery({
    queryKey: ['library', tab, q, sort, page],
    queryFn: () =>
      api.listLibrary({ tab: tab as LibraryTab, q: q || undefined, sort, page, size: PAGE_SIZE }),
    enabled: onLibraryTab,
    retry: false,
    placeholderData: keepPreviousData,
  });

  // 「我发表的」待确认数：给 tab 标签上的数量徽标用（与 PublicationsTab 内的
  // pending 列表共用同一个 queryKey，切进去时直接复用缓存）
  const pendingBadgeQuery = useQuery({
    queryKey: ['publications', 'pending', 1],
    queryFn: () => api.listPublications({ status: 'pending', page: 1, size: PUBLICATIONS_PAGE_SIZE }),
    retry: false,
  });
  const pendingCount = pendingBadgeQuery.data?.counts.pending ?? 0;

  // 「我发表的」署名信息（与 PublicationsTab 原查询同 key，缓存互通）：
  // 未填 → 表单占满整卡；已填 → 双栏
  const profileQuery = useQuery({
    queryKey: ['author-profile'],
    queryFn: () => api.getAuthorProfile(),
    enabled: tab === 'publications',
    retry: false,
  });
  const profile = profileQuery.data ?? null;

  const data = listQuery.data;
  const entries = data?.items ?? [];
  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.size)) : 1;

  // 选中条目若还在当前页，用列表里的最新数据（收藏态等会实时变化）
  const shownEntry = selEntry ? (entries.find((e) => e.id === selEntry.id) ?? selEntry) : null;

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['library'] });
    // 阅读页书签按钮共享的状态缓存也一并失效
    void queryClient.invalidateQueries({ queryKey: ['library-state'] });
  };

  const toggleMutation = useMutation({
    mutationFn: (entry: LibraryEntry) =>
      entry.saved
        ? api.removeLibraryEntry(entry.id, 'unsave')
        : api.saveToLibrary({ entry_id: entry.id }).then(() => undefined),
    onSuccess: (_d, entry) => {
      toast(entry.saved ? tr('已取消收藏', 'Unsaved') : tr('已收藏', 'Saved'), 'ok');
      // 选中的就是这条时同步右栏快照里的收藏态
      setSelEntry((old) => (old && old.id === entry.id ? { ...old, saved: !entry.saved } : old));
      invalidate();
    },
    onError: (e) => toast(`${tr('操作失败：', 'Action failed: ')}${errText(e)}`, 'error'),
  });

  const purgeMutation = useMutation({
    mutationFn: (entry: LibraryEntry) => api.removeLibraryEntry(entry.id, 'purge'),
    onSuccess: (_d, entry) => {
      toast(tr('已删除这条记录', 'Record deleted'), 'ok');
      // 删除的是当前选中项 → 清空右栏
      setSelEntry((old) => (old && old.id === entry.id ? null : old));
      invalidate();
    },
    onError: (e) => toast(`${tr('删除失败：', 'Delete failed: ')}${errText(e)}`, 'error'),
  });

  const clearMutation = useMutation({
    mutationFn: () => api.clearLibraryVisits(),
    onSuccess: () => {
      setClearOpen(false);
      setSelEntry(null);
      toast(tr('浏览记录已清空', 'History cleared'), 'ok');
      invalidate();
    },
    onError: (e) => toast(`${tr('清空失败：', 'Clear failed: ')}${errText(e)}`, 'error'),
  });

  const busy = toggleMutation.isPending || purgeMutation.isPending;

  return (
    <div
      className="page fadeup"
      style={{ maxWidth: 1360, display: 'flex', flexDirection: 'column', height: '100%', paddingBottom: 24 }}
    >
      <PageHead eyebrow="Polaris · My Library" title={tr('我的文献库', 'My Library')} dense />

      {/* —— tab 行 —— */}
      <div className="row" style={{ marginBottom: 14 }}>
        <Segmented<PageTab>
          options={[
            { v: 'saved', label: tr('我的收藏', 'Saved') },
            { v: 'history', label: tr('浏览记录', 'History') },
            {
              v: 'publications',
              label: (
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                  {tr('我发表的', 'My Publications')}
                  {pendingCount > 0 && (
                    <span
                      className="mono"
                      style={{
                        minWidth: 16,
                        height: 16,
                        padding: '0 4px',
                        borderRadius: 8,
                        background: 'var(--accent)',
                        color: '#fff',
                        fontSize: 10,
                        fontWeight: 700,
                        display: 'inline-flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                      }}
                    >
                      {pendingCount}
                    </span>
                  )}
                </span>
              ),
            },
          ]}
          value={tab}
          onChange={setTab}
        />
      </div>

      {/* —— 卡片容器（同文献追踪的论文库外壳） —— */}
      <div
        className="card"
        style={{ overflow: 'hidden', display: 'flex', flexDirection: 'column', flex: 1, minHeight: 480 }}
      >
        {tab === 'publications' ? (
          /* ======== 我发表的 ======== */
          profileQuery.isLoading ? (
            <div className="empty" style={{ margin: 'auto' }}>{tr('加载中…', 'Loading…')}</div>
          ) : profileQuery.isError ? (
            <div className="scroll" style={{ overflowY: 'auto', flex: 1, padding: '18px 20px 28px' }}>
              <EmptyState
                compact
                icon="x"
                title={tr('署名信息暂时加载不出来', 'Failed to load your author info')}
                desc={tr('后端不可用或接口尚未就绪，稍后再试。', 'Backend unavailable or API not ready — try again later.')}
                action={
                  <button className="btn btn-soft sm" onClick={() => void profileQuery.refetch()}>
                    {tr('重试', 'Retry')}
                  </button>
                }
              />
            </div>
          ) : !profile || editingAuthor ? (
            /* 未填署名信息 / 修改中 → 表单占满整卡（不用双栏） */
            <div className="scroll" style={{ overflowY: 'auto', flex: 1, padding: '18px 20px 28px' }}>
              <AuthorBindWizard
                profile={profile}
                onDone={() => setEditingAuthor(false)}
                onCancel={profile ? () => setEditingAuthor(false) : undefined}
              />
            </div>
          ) : (
            /* 已填 → 左栏列表 + 待确认，右栏详情 */
            <div className="split">
              <div className="split-list">
                <div className="scroll" style={{ overflowY: 'auto', flex: 1, padding: '14px 14px 24px' }}>
                  <PublicationsTab
                    profile={profile}
                    selectedId={selPub?.id ?? null}
                    onSelect={setSelPub}
                    onEditProfile={() => setEditingAuthor(true)}
                  />
                </div>
              </div>
              <div className="split-detail">
                {selPub ? (
                  <LibraryDetailPane key={selPub.id} paperId={selPub.paper_id} snapshot={pubSnapshot(selPub)} />
                ) : (
                  <PickHint />
                )}
              </div>
            </div>
          )
        ) : (
          /* ======== 我的收藏 / 浏览记录 ======== */
          <div className="split">
            {/* —— 左：列表 —— */}
            <div className="split-list">
              {/* 工具栏：搜索 + 排序 + 计数 + 清空记录 */}
              <div style={{ padding: '12px 14px 10px', borderBottom: '0.5px solid var(--border)' }}>
                <SearchInput
                  value={qInput}
                  onChange={setQInput}
                  placeholder={tr('搜索标题 / 作者…', 'Search title / authors…')}
                />
                <div className="row gap8" style={{ marginTop: 10 }}>
                  <Segmented<LibrarySort>
                    options={SORTS.map((s) => ({ v: s.v, label: tr(s.zh, s.en) }))}
                    value={sort}
                    onChange={setSort}
                  />
                  <span
                    className="mono"
                    style={{ marginLeft: 'auto', fontSize: 10.5, color: 'var(--text-3)', flexShrink: 0 }}
                  >
                    {data ? tr(`共 ${data.total} 条`, `${data.total} total`) : ''}
                  </span>
                </div>
                {tab === 'history' && (
                  <div className="row" style={{ marginTop: 8 }}>
                    <button
                      className="btn btn-ghost sm"
                      style={{ marginLeft: 'auto', height: 26 }}
                      disabled={clearMutation.isPending || (data !== undefined && data.total === 0)}
                      onClick={() => setClearOpen(true)}
                    >
                      <Icon name="trash" size={13} />
                      {tr('清空记录', 'Clear history')}
                    </button>
                  </div>
                )}
              </div>

              {/* 列表（自滚动） */}
              <div className="scroll" style={{ overflowY: 'auto', flex: 1 }}>
                {listQuery.isLoading ? (
                  <div className="empty">{tr('加载中…', 'Loading…')}</div>
                ) : listQuery.isError ? (
                  <EmptyState
                    compact
                    icon="x"
                    title={tr('文献库暂时加载不出来', 'Failed to load your library')}
                    desc={tr('后端不可用或接口尚未就绪，稍后再试。', 'Backend unavailable or API not ready — try again later.')}
                    action={
                      <button className="btn btn-soft sm" onClick={() => void listQuery.refetch()}>
                        {tr('重试', 'Retry')}
                      </button>
                    }
                  />
                ) : entries.length === 0 ? (
                  <EmptyState
                    compact
                    icon="bookmark"
                    title={
                      q
                        ? tr('没有匹配的文献', 'No matching papers')
                        : tab === 'saved'
                          ? tr('还没有收藏的文献', 'Nothing saved yet')
                          : tr('还没有浏览记录', 'No reading history yet')
                    }
                    desc={
                      q
                        ? tr('换个关键词试试。', 'Try a different keyword.')
                        : tab === 'saved'
                          ? tr(
                              '在论文阅读页点右上角的书签按钮，就能把它收进这里。',
                              'Tap the bookmark button on any paper reading page to save it here.',
                            )
                          : tr(
                              '打开任意论文的阅读页后，会自动记录在这里。',
                              'Papers you open in the reader will show up here automatically.',
                            )
                    }
                  />
                ) : (
                  entries.map((entry) => (
                    <EntryRow
                      key={entry.id}
                      entry={entry}
                      tab={tab}
                      active={entry.id === selEntry?.id}
                      busy={busy}
                      onSelect={() => setSelEntry(entry)}
                      onToggleSave={() => toggleMutation.mutate(entry)}
                      onPurge={() => purgeMutation.mutate(entry)}
                    />
                  ))
                )}
              </div>

              {/* 底部分页栏 */}
              {data && data.total > PAGE_SIZE && (
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

            {/* —— 右：详情 —— */}
            <div className="split-detail">
              {shownEntry ? (
                <LibraryDetailPane
                  key={shownEntry.id}
                  paperId={shownEntry.last_paper_id}
                  snapshot={entrySnapshot(shownEntry)}
                />
              ) : (
                <PickHint />
              )}
            </div>
          </div>
        )}
      </div>

      {/* —— 清空浏览记录确认 —— */}
      <ConfirmModal
        open={clearOpen}
        onClose={() => setClearOpen(false)}
        title={tr('清空浏览记录？', 'Clear reading history?')}
        message={tr(
          '将删除全部浏览记录；已收藏的文献会保留在「我的收藏」里。此操作不可撤销。',
          'All reading history will be deleted. Saved papers stay in the Saved tab. This cannot be undone.',
        )}
        confirmText={tr('清空', 'Clear')}
        danger
        busy={clearMutation.isPending}
        onConfirm={() => clearMutation.mutate()}
      />
    </div>
  );
}
