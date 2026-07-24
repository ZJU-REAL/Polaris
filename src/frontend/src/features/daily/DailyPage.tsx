import { Fragment, useEffect, useState } from 'react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { EmptyState } from '../../components/ui/EmptyState';
import { api, type DailyPaperItem, type DailySort } from '../../lib/api';
import { fmtTime } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { SearchInput, useDebounced } from '../wiki/shared';
import { DailyLikes } from './DailyLikes';
import { CollectTreeModal, type CollectPaperRef } from './CollectTreeModal';

/* ============================================================
   /daily — 每日新论文：arxiv 每日新提交（订阅分类内），保留最近 7 天。
   双栏主从布局对齐共享库浏览（LibraryBrowse）：
   左栏按日期分组的列表（排序 / 搜索 / 分页 / 行内点赞），右栏选中论文详情。
   ============================================================ */

const PAGE_SIZE = 20;

const EN_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** 'YYYY-MM-DD' → 「7月24日 · 32 篇」/ "Jul 24 · 32 papers"（count 未知时只显示日期）。 */
function dayLabel(iso: string, count: number | undefined): string {
  const parts = iso.split('-');
  const m = Number(parts[1] ?? 0);
  const d = Number(parts[2] ?? 0);
  const en = `${EN_MONTHS[m - 1] ?? iso} ${d}`;
  if (count === undefined) return tr(`${m}月${d}日`, en);
  return tr(`${m}月${d}日 · ${count} 篇`, `${en} · ${count} papers`);
}

/** 作者行：前 3 名 + et al。 */
function authorsBrief(p: DailyPaperItem): string {
  const names = p.authors.map((a) => a.name).filter(Boolean);
  if (names.length === 0) return '';
  return names.length > 3 ? `${names.slice(0, 3).join(', ')} et al.` : names.join(', ');
}

function DailyRow({
  p,
  active,
  onClick,
}: {
  p: DailyPaperItem;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <div
      onClick={onClick}
      style={{
        padding: '11px 14px 11px 16px',
        cursor: 'pointer',
        borderBottom: '0.5px solid var(--border)',
        background: active ? 'var(--accent-soft)' : 'transparent',
        borderLeft: active ? '2px solid var(--accent)' : '2px solid transparent',
        transition: 'background 0.12s',
      }}
    >
      <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start' }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.35 }}>{p.title}</div>
          <div className="row gap8" style={{ marginTop: 5, fontSize: 11, color: 'var(--text-3)' }}>
            <span className="pill sm mono" style={{ background: 'var(--surface-3)', flexShrink: 0 }}>
              {p.primary_category}
            </span>
            {p.announce_type === 'cross' && (
              <span
                className="pill sm"
                style={{ background: 'var(--warn-bg)', color: 'var(--warn-tx)', flexShrink: 0 }}
              >
                {tr('转投', 'cross')}
              </span>
            )}
            <span
              style={{ flex: 1, minWidth: 0, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}
              title={p.authors.map((a) => a.name).join(', ')}
            >
              {authorsBrief(p)}
            </span>
          </div>
        </div>
        {/* 行右侧点赞区：[♥][头像堆][+N][数字] */}
        <div style={{ paddingTop: 1 }}>
          <DailyLikes item={p} />
        </div>
      </div>
    </div>
  );
}

function DailyDetailPane({
  entryId,
  onCollect,
}: {
  entryId: string;
  onCollect: (p: CollectPaperRef) => void;
}) {
  const { data: paper, isLoading, isError } = useQuery({
    queryKey: ['daily-paper', entryId],
    queryFn: () => api.getDailyPaper(entryId),
    retry: false,
  });

  if (isLoading) return <div className="empty">{tr('加载论文详情…', 'Loading paper…')}</div>;
  if (isError || !paper) {
    return (
      <EmptyState
        compact
        icon="x"
        title={tr('无法加载论文详情', 'Failed to load paper')}
        desc={tr('后端不可用或该论文已过期。', 'Backend unavailable or the paper has expired.')}
      />
    );
  }

  const authors = paper.authors.map((a) => a.name).filter(Boolean).join(', ');

  return (
    <div className="scroll fadeup" key={paper.entry_id} style={{ overflowY: 'auto', flex: 1, padding: '26px 32px 60px' }}>
      <div className="row gap8 wrap" style={{ marginBottom: 8 }}>
        {[paper.primary_category, ...paper.categories.filter((c) => c !== paper.primary_category)].map((c) => (
          <span key={c} className="pill sm mono" style={{ background: 'var(--surface-3)' }}>
            {c}
          </span>
        ))}
        {paper.announce_type === 'cross' && (
          <span className="pill sm" style={{ background: 'var(--warn-bg)', color: 'var(--warn-tx)' }}>
            {tr('转投', 'cross')}
          </span>
        )}
        {paper.arxiv_id && (
          <span className="pill sm mono" style={{ background: 'var(--surface-3)' }}>
            arXiv:{paper.arxiv_id}
          </span>
        )}
      </div>

      <h1 style={{ fontSize: 21, fontWeight: 680, lineHeight: 1.3, margin: '2px 0 6px', letterSpacing: '-0.01em' }}>
        {paper.url ? (
          <a href={paper.url} target="_blank" rel="noreferrer" style={{ color: 'inherit' }}>
            {paper.title}
            <Icon name="link" size={14} style={{ display: 'inline-block', marginLeft: 6, verticalAlign: 'baseline', color: 'var(--text-3)' }} />
          </a>
        ) : (
          paper.title
        )}
      </h1>
      {authors && <div style={{ fontSize: 12.5, color: 'var(--text-3)', marginBottom: 6 }}>{authors}</div>}
      {paper.published_at && (
        <div style={{ fontSize: 11.5, color: 'var(--text-4)', marginBottom: 14 }}>
          {tr('发布于', 'Published')} {fmtTime(paper.published_at)}
        </div>
      )}

      <div className="row gap8" style={{ marginBottom: 18 }}>
        <button
          className="btn btn-primary sm"
          onClick={() => onCollect({ paper_id: paper.paper_id, entry_id: paper.entry_id, title: paper.title })}
        >
          <Icon name="plus" size={13} />
          {tr('收进文献库', 'Add to libraries')}
        </button>
        <DailyLikes item={paper} />
      </div>

      {paper.abstract ? (
        <div className="card card-pad" style={{ background: 'var(--surface-2)' }}>
          <div className="row gap8" style={{ marginBottom: 8 }}>
            <Icon name="file" size={14} style={{ color: 'var(--accent)' }} />
            <span style={{ fontSize: 12, fontWeight: 700 }}>{tr('摘要', 'Abstract')}</span>
          </div>
          <p style={{ fontSize: 13.5, lineHeight: 1.7, margin: 0 }}>{paper.abstract}</p>
        </div>
      ) : (
        <div className="empty" style={{ padding: 20 }}>{tr('这篇还没有摘要。', 'No abstract for this paper.')}</div>
      )}
    </div>
  );
}

export function DailyPage() {
  const [qInput, setQInput] = useState('');
  const q = useDebounced(qInput.trim());
  const [sort, setSort] = useState<DailySort>('likes');
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [collectPaper, setCollectPaper] = useState<CollectPaperRef | null>(null);
  const [collectOpen, setCollectOpen] = useState(false);

  useEffect(() => setPage(1), [q, sort]);

  const daysQuery = useQuery({
    queryKey: ['daily-days'],
    queryFn: () => api.listDailyDays(),
    retry: false,
    staleTime: 60_000,
  });
  const dayCount = new Map((daysQuery.data ?? []).map((d) => [d.date, d.count] as const));

  const categoriesQuery = useQuery({
    queryKey: ['daily-categories'],
    queryFn: () => api.getDailyCategories(),
    retry: false,
    staleTime: 300_000,
  });

  const listQuery = useQuery({
    queryKey: ['daily-papers', sort, page, q],
    queryFn: () => api.listDailyPapers({ sort, page, size: PAGE_SIZE, q: q || undefined }),
    retry: false,
    placeholderData: keepPreviousData,
  });
  const items = listQuery.data?.items ?? [];
  const total = listQuery.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  // 首条自动选中
  const firstId = items[0]?.entry_id ?? null;
  useEffect(() => {
    if (!selectedId && firstId) setSelectedId(firstId);
  }, [selectedId, firstId]);

  const openCollect = (p: CollectPaperRef) => {
    setCollectPaper(p);
    setCollectOpen(true);
  };

  return (
    <div
      className="page fadeup"
      style={{ maxWidth: 1360, display: 'flex', flexDirection: 'column', height: '100%', paddingBottom: 24 }}
    >
      <PageHead
        eyebrow="Polaris · Daily Papers"
        title={tr('每日新论文', 'Daily Papers')}
        sub={tr('arxiv 每日新提交，保留最近 7 天', 'New arxiv submissions, kept for the last 7 days')}
        right={
          (categoriesQuery.data?.categories.length ?? 0) > 0 ? (
            <div className="row gap6 wrap" style={{ justifyContent: 'flex-end', maxWidth: 420 }}>
              <span style={{ fontSize: 11, color: 'var(--text-4)' }}>{tr('订阅分类', 'Subscribed')}</span>
              {categoriesQuery.data?.categories.map((c) => (
                <span key={c} className="pill sm mono" style={{ background: 'var(--surface-3)' }}>
                  {c}
                </span>
              ))}
            </div>
          ) : undefined
        }
      />

      <div
        className="card"
        style={{ overflow: 'hidden', display: 'flex', flexDirection: 'column', flex: 1, minHeight: 480 }}
      >
        <div className="split">
          {/* —— 左：按日期分组的列表 —— */}
          <div className="split-list">
            <div style={{ padding: '12px 14px 10px', borderBottom: '0.5px solid var(--border)' }}>
              <div className="row gap8">
                <SearchInput
                  value={qInput}
                  onChange={setQInput}
                  placeholder={tr('搜标题 / 摘要 / 作者…', 'Search title / abstract / authors…')}
                />
                <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)', flexShrink: 0 }}>
                  {total ? tr(`${total} 篇`, `${total}`) : ''}
                </span>
              </div>
              <div className="row gap6" style={{ marginTop: 10 }}>
                <span className={`chip${sort === 'likes' ? ' on' : ''}`} onClick={() => setSort('likes')}>
                  {tr('按点赞', 'Most liked')}
                </span>
                <span className={`chip${sort === 'date' ? ' on' : ''}`} onClick={() => setSort('date')}>
                  {tr('按时间', 'Newest')}
                </span>
              </div>
            </div>

            <div className="scroll" style={{ overflowY: 'auto', flex: 1 }}>
              {listQuery.isLoading ? (
                <div className="empty">{tr('加载论文…', 'Loading papers…')}</div>
              ) : listQuery.isError ? (
                <EmptyState
                  compact
                  icon="x"
                  title={tr('无法加载每日新论文', 'Failed to load daily papers')}
                  desc={tr('后端不可用或接口尚未就绪，稍后重试。', 'Backend unavailable — try again later.')}
                />
              ) : items.length === 0 ? (
                <EmptyState
                  compact
                  icon="book"
                  title={q ? tr('没有匹配的论文', 'No matching papers') : tr('今天还没有新论文', 'No new papers yet')}
                  desc={
                    q
                      ? tr('换个关键词试试。', 'Try a different keyword.')
                      : tr(
                          '今天还没有新论文。arxiv 周末不发布新提交。',
                          'No new papers yet. arxiv does not announce on weekends.',
                        )
                  }
                />
              ) : (
                items.map((p, i) => {
                  // 与上一条日期不同 → 插入粘性日期头
                  const prev = items[i - 1];
                  const newDay = i === 0 || prev?.feed_date !== p.feed_date;
                  return (
                    <Fragment key={p.entry_id}>
                      {newDay && (
                        <div
                          style={{
                            position: 'sticky',
                            top: 0,
                            zIndex: 3,
                            padding: '6px 16px',
                            fontSize: 11,
                            fontWeight: 700,
                            color: 'var(--text-3)',
                            background: 'var(--surface-2)',
                            borderBottom: '0.5px solid var(--border)',
                          }}
                        >
                          {dayLabel(p.feed_date, dayCount.get(p.feed_date))}
                        </div>
                      )}
                      <DailyRow p={p} active={p.entry_id === selectedId} onClick={() => setSelectedId(p.entry_id)} />
                    </Fragment>
                  );
                })
              )}
            </div>

            {pages > 1 && (
              <div
                className="row gap8"
                style={{ padding: '8px 14px', borderTop: '0.5px solid var(--border)', justifyContent: 'center' }}
              >
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

          {/* —— 右：详情 —— */}
          <div className="split-detail">
            {selectedId ? (
              <DailyDetailPane entryId={selectedId} onCollect={openCollect} />
            ) : (
              <div className="empty" style={{ margin: 'auto' }}>
                {tr('选择左侧论文查看详情', 'Select a paper to view details')}
              </div>
            )}
          </div>
        </div>
      </div>

      {collectPaper && (
        <CollectTreeModal paper={collectPaper} open={collectOpen} onClose={() => setCollectOpen(false)} />
      )}
    </div>
  );
}
