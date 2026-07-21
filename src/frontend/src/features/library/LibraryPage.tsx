import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { Segmented } from '../../components/ui/Segmented';
import { EmptyState } from '../../components/ui/EmptyState';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { SelectMenu } from '../../components/ui/SelectMenu';
import { toast } from '../../components/ui/Toast';
import { api, type LibraryEntry, type LibrarySort, type LibraryTab } from '../../lib/api';
import { fmtRelative } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { SearchInput, useDebounced } from '../wiki/shared';
import { PublicationsTab, PUBLICATIONS_PAGE_SIZE } from './PublicationsTab';

/* ============================================================
   /library — 我的文献库（跨研究方向的个人空间）：
   「我的收藏」+「浏览记录」+「我发表的」三个 tab；
   收藏/记录带搜索、排序、分页，行点击回到源方向的阅读页
   （源方向已删除时走外链）；「我发表的」见 PublicationsTab。
   ============================================================ */

const PAGE_SIZE = 20;

/** 页面级 tab：库内两个 tab + 「我发表的」。 */
type PageTab = LibraryTab | 'publications';

// 模块级常量不调 tr()：保留 zh/en 字段，渲染处再 tr
const SORTS: { v: LibrarySort; zh: string; en: string }[] = [
  { v: 'recent', zh: '最近浏览', en: 'Recently visited' },
  { v: 'title', zh: '标题', en: 'Title' },
  { v: 'visits', zh: '看过次数', en: 'Visit count' },
];

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function EntryRow({
  entry,
  tab,
  busy,
  onOpen,
  onToggleSave,
  onPurge,
}: {
  entry: LibraryEntry;
  tab: LibraryTab;
  busy: boolean;
  onOpen: () => void;
  onToggleSave: () => void;
  onPurge: () => void;
}) {
  const authors = entry.authors.map((a) => a.name).join(', ');
  const metaBits = [authors, [entry.venue, entry.year].filter(Boolean).join(' · ')].filter(Boolean);
  const openable = entry.last_paper_id !== null || !!entry.url;
  return (
    <div
      className="card"
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onOpen();
        }
      }}
      style={{
        padding: '12px 16px',
        display: 'flex',
        alignItems: 'flex-start',
        gap: 14,
        cursor: openable ? 'pointer' : 'default',
      }}
    >
      {/* —— 左：标题 / 作者 / venue·年份 / tldr —— */}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="row gap8" style={{ minWidth: 0 }}>
          <span
            title={entry.title}
            style={{
              fontSize: 13.5,
              fontWeight: 650,
              letterSpacing: '-0.01em',
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              minWidth: 0,
            }}
          >
            {entry.title}
          </span>
          {entry.last_paper_id === null && (
            <span style={{ fontSize: 11, color: 'var(--text-4)', flexShrink: 0 }}>
              {tr('源方向已删除', 'Source direction deleted')}
            </span>
          )}
        </div>
        {metaBits.length > 0 && (
          <div
            title={metaBits.join(' — ')}
            style={{
              fontSize: 11.5,
              color: 'var(--text-3)',
              marginTop: 3,
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >
            {metaBits.join(' — ')}
          </div>
        )}
        {entry.tldr && (
          <div
            style={{
              fontSize: 12,
              color: 'var(--text-2)',
              marginTop: 4,
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >
            {entry.tldr}
          </div>
        )}
      </div>

      {/* —— 右：浏览信息 + 操作 —— */}
      <div className="row gap8" style={{ flexShrink: 0, alignItems: 'center' }}>
        {entry.visit_count > 0 && (
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>
            {fmtRelative(entry.last_visited_at)}
            {' · '}
            {tr(`看过 ${entry.visit_count} 次`, `${entry.visit_count} visits`)}
          </span>
        )}
        <button
          className="icon-btn"
          disabled={busy}
          title={entry.saved ? tr('取消收藏', 'Unsave') : tr('收藏', 'Save')}
          style={{ color: entry.saved ? 'var(--accent)' : 'var(--text-3)' }}
          onClick={(e) => {
            e.stopPropagation();
            onToggleSave();
          }}
        >
          <Icon name={entry.saved ? 'bookmarkFill' : 'bookmark'} size={15} />
        </button>
        {tab === 'history' && (
          <button
            className="icon-btn"
            disabled={busy}
            title={tr('彻底删除这条记录', 'Delete this record')}
            style={{ color: 'var(--text-3)' }}
            onClick={(e) => {
              e.stopPropagation();
              onPurge();
            }}
          >
            <Icon name="trash" size={15} />
          </button>
        )}
      </div>
    </div>
  );
}

export function LibraryPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [tab, setTab] = useState<PageTab>('saved');
  const [qInput, setQInput] = useState('');
  const q = useDebounced(qInput.trim());
  const [sort, setSort] = useState<LibrarySort>('recent');
  const [page, setPage] = useState(1);
  const [clearOpen, setClearOpen] = useState(false);

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
  const data = listQuery.data;
  const entries = data?.items ?? [];
  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.size)) : 1;

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
      invalidate();
    },
    onError: (e) => toast(`${tr('操作失败：', 'Action failed: ')}${errText(e)}`, 'error'),
  });

  const purgeMutation = useMutation({
    mutationFn: (entry: LibraryEntry) => api.removeLibraryEntry(entry.id, 'purge'),
    onSuccess: () => {
      toast(tr('已删除这条记录', 'Record deleted'), 'ok');
      invalidate();
    },
    onError: (e) => toast(`${tr('删除失败：', 'Delete failed: ')}${errText(e)}`, 'error'),
  });

  const clearMutation = useMutation({
    mutationFn: () => api.clearLibraryVisits(),
    onSuccess: () => {
      setClearOpen(false);
      toast(tr('浏览记录已清空', 'History cleared'), 'ok');
      invalidate();
    },
    onError: (e) => toast(`${tr('清空失败：', 'Clear failed: ')}${errText(e)}`, 'error'),
  });

  const busy = toggleMutation.isPending || purgeMutation.isPending;

  const openEntry = (entry: LibraryEntry) => {
    if (entry.last_paper_id) {
      navigate(`/papers/${entry.last_paper_id}/read`);
    } else if (entry.url) {
      window.open(entry.url, '_blank', 'noopener');
    }
  };

  return (
    <div className="page fadeup">
      <PageHead
        eyebrow="Polaris · My Library"
        title={tr('我的文献库', 'My Library')}
        sub={tr(
          '跨研究方向的个人空间：收藏的文献、看过的论文和你自己发表的论文都在这里。',
          'Your personal space across research directions: saved papers, reading history, and your own publications.',
        )}
      />

      {/* —— tab + 工具行 —— */}
      <div className="row gap12 wrap" style={{ marginBottom: 14 }}>
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
        {onLibraryTab && (
          <>
            <div style={{ maxWidth: 340, flex: 1, minWidth: 180 }}>
              <SearchInput
                value={qInput}
                onChange={setQInput}
                placeholder={tr('搜索标题 / 作者…', 'Search title / authors…')}
              />
            </div>
            <SelectMenu
              value={sort}
              options={SORTS.map((s) => ({ value: s.v, label: tr(s.zh, s.en) }))}
              onChange={(v) => setSort(v as LibrarySort)}
              wrapStyle={{ width: 140 }}
              style={{ height: 32, fontSize: 12.5 }}
            />
            <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)', marginLeft: 'auto' }}>
              {data ? tr(`共 ${data.total} 条`, `${data.total} total`) : ''}
            </span>
            {tab === 'history' && (
              <button
                className="btn btn-ghost sm"
                disabled={clearMutation.isPending || (data !== undefined && data.total === 0)}
                onClick={() => setClearOpen(true)}
              >
                <Icon name="trash" size={13} />
                {tr('清空记录', 'Clear history')}
              </button>
            )}
          </>
        )}
      </div>

      {/* —— 我发表的 —— */}
      {tab === 'publications' && <PublicationsTab />}

      {/* —— 收藏 / 浏览记录列表 —— */}
      {tab === 'publications' ? null : listQuery.isLoading ? (
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
        <div className="col" style={{ gap: 10 }}>
          {entries.map((entry) => (
            <EntryRow
              key={entry.id}
              entry={entry}
              tab={tab}
              busy={busy}
              onOpen={() => openEntry(entry)}
              onToggleSave={() => toggleMutation.mutate(entry)}
              onPurge={() => purgeMutation.mutate(entry)}
            />
          ))}
        </div>
      )}

      {/* —— 分页（收藏 / 浏览记录） —— */}
      {onLibraryTab && data && data.total > PAGE_SIZE && (
        <div className="row gap12" style={{ justifyContent: 'center', marginTop: 16 }}>
          <button className="btn btn-ghost sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
            <Icon name="chevron" size={12} style={{ transform: 'rotate(180deg)' }} />
            {tr('上一页', 'Prev')}
          </button>
          <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>
            {tr(`第 ${page} / ${totalPages} 页`, `Page ${page} / ${totalPages}`)}
          </span>
          <button className="btn btn-ghost sm" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>
            {tr('下一页', 'Next')}
            <Icon name="chevron" size={12} />
          </button>
        </div>
      )}

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
