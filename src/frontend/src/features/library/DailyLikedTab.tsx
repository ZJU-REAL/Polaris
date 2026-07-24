import { useState } from 'react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import { api, type DailyPaperItem } from '../../lib/api';
import { fmtRelative, fmtTime } from '../../lib/format';
import { tr } from '../../lib/i18n';

/* ============================================================
   我的文献库 ·「我赞过的」tab：每日新论文里点过赞的论文。
   池子只保留 7 天，过期条目会随之从这里消失。
   左列表 + 右轻量详情（标题链接 / 作者 / 摘要）。
   ============================================================ */

const PAGE_SIZE = 20;

// 点赞爱心的红色（与每日新论文页一致的局部常量，不走主题 token）
const HEART_RED = '#e0245e';

function LikedRow({
  p,
  active,
  onSelect,
}: {
  p: DailyPaperItem;
  active: boolean;
  onSelect: () => void;
}) {
  const authors = p.authors.map((a) => a.name).filter(Boolean).join(', ');
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
      <div className="row gap8" style={{ marginBottom: 5 }}>
        <span className="mono" style={{ fontSize: 10.5, color: active ? 'var(--accent-text)' : 'var(--text-3)' }}>
          {p.arxiv_id ?? p.primary_category}
        </span>
        {p.year !== null && (
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>{p.year}</span>
        )}
        <span className="row gap6" style={{ marginLeft: 'auto', fontSize: 10.5, color: 'var(--text-4)', flexShrink: 0 }}>
          <span className="row" style={{ gap: 3 }}>
            <Icon name="heartFill" size={11} style={{ color: HEART_RED }} />
            <span className="mono">{p.like_count}</span>
          </span>
          {p.liked_at && (
            <span className="mono">{tr(`赞于 ${fmtRelative(p.liked_at)}`, `liked ${fmtRelative(p.liked_at)}`)}</span>
          )}
        </span>
      </div>
      <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.35, color: 'var(--text)' }}>{p.title}</div>
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
    </div>
  );
}

/** 轻量详情：标题链接 / 作者全列 / 分类 / 摘要（daily 条目与库条目结构不同，单独渲染）。 */
function LikedDetail({ p }: { p: DailyPaperItem }) {
  const authors = p.authors.map((a) => a.name).filter(Boolean).join(', ');
  return (
    <div className="scroll fadeup" key={p.entry_id} style={{ overflowY: 'auto', flex: 1, padding: '26px 32px 60px' }}>
      <div className="row gap8 wrap" style={{ marginBottom: 8 }}>
        <span className="pill sm mono" style={{ background: 'var(--surface-3)' }}>{p.primary_category}</span>
        {p.arxiv_id && (
          <span className="pill sm mono" style={{ background: 'var(--surface-3)' }}>arXiv:{p.arxiv_id}</span>
        )}
        {p.year !== null && <span className="pill sm" style={{ background: 'var(--surface-3)' }}>{p.year}</span>}
      </div>
      <h1 style={{ fontSize: 21, fontWeight: 680, lineHeight: 1.3, margin: '2px 0 6px', letterSpacing: '-0.01em' }}>
        {p.url ? (
          <a href={p.url} target="_blank" rel="noreferrer" style={{ color: 'inherit' }}>
            {p.title}
            <Icon name="link" size={14} style={{ display: 'inline-block', marginLeft: 6, verticalAlign: 'baseline', color: 'var(--text-3)' }} />
          </a>
        ) : (
          p.title
        )}
      </h1>
      {authors && <div style={{ fontSize: 12.5, color: 'var(--text-3)', marginBottom: 6 }}>{authors}</div>}
      {p.published_at && (
        <div style={{ fontSize: 11.5, color: 'var(--text-4)', marginBottom: 14 }}>
          {tr('发布于', 'Published')} {fmtTime(p.published_at)}
        </div>
      )}
      {p.abstract ? (
        <div className="card card-pad" style={{ background: 'var(--surface-2)' }}>
          <div className="row gap8" style={{ marginBottom: 8 }}>
            <Icon name="file" size={14} style={{ color: 'var(--accent)' }} />
            <span style={{ fontSize: 12, fontWeight: 700 }}>{tr('摘要', 'Abstract')}</span>
          </div>
          <p style={{ fontSize: 13.5, lineHeight: 1.7, margin: 0 }}>{p.abstract}</p>
        </div>
      ) : (
        <div className="empty" style={{ padding: 20 }}>{tr('这篇还没有摘要。', 'No abstract for this paper.')}</div>
      )}
    </div>
  );
}

export function DailyLikedTab() {
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const listQuery = useQuery({
    queryKey: ['daily-liked', page],
    queryFn: () => api.listMyDailyLiked({ page, size: PAGE_SIZE }),
    retry: false,
    placeholderData: keepPreviousData,
  });
  const items = listQuery.data?.items ?? [];
  const total = listQuery.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const selected = items.find((p) => p.entry_id === selectedId) ?? null;

  return (
    <div className="split">
      {/* —— 左：列表 —— */}
      <div className="split-list">
        <div className="scroll" style={{ overflowY: 'auto', flex: 1 }}>
          {listQuery.isLoading ? (
            <div className="empty">{tr('加载中…', 'Loading…')}</div>
          ) : listQuery.isError ? (
            <EmptyState
              compact
              icon="x"
              title={tr('赞过的论文暂时加载不出来', 'Failed to load liked papers')}
              desc={tr('后端不可用或接口尚未就绪，稍后再试。', 'Backend unavailable or API not ready — try again later.')}
              action={
                <button className="btn btn-soft sm" onClick={() => void listQuery.refetch()}>
                  {tr('重试', 'Retry')}
                </button>
              }
            />
          ) : items.length === 0 ? (
            <EmptyState
              compact
              icon="heart"
              title={tr('还没有赞过的论文', 'No liked papers yet')}
              desc={tr(
                '还没有赞过的论文。每日新论文只保留 7 天，过期后这里也会跟着清空。',
                'No liked papers yet. Daily papers are kept for 7 days; expired ones disappear from here too.',
              )}
            />
          ) : (
            items.map((p) => (
              <LikedRow
                key={p.entry_id}
                p={p}
                active={p.entry_id === selectedId}
                onSelect={() => setSelectedId(p.entry_id)}
              />
            ))
          )}
        </div>

        {pages > 1 && (
          <div
            className="row gap12"
            style={{ padding: '9px 14px', borderTop: '0.5px solid var(--border)', justifyContent: 'center', flexShrink: 0 }}
          >
            <button className="btn btn-ghost sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
              <Icon name="chevron" size={12} style={{ transform: 'rotate(180deg)' }} />
              {tr('上一页', 'Prev')}
            </button>
            <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>
              {tr(`第 ${page} / ${pages} 页`, `Page ${page} / ${pages}`)}
            </span>
            <button className="btn btn-ghost sm" disabled={page >= pages} onClick={() => setPage((p) => p + 1)}>
              {tr('下一页', 'Next')}
              <Icon name="chevron" size={12} />
            </button>
          </div>
        )}
      </div>

      {/* —— 右：轻量详情 —— */}
      <div className="split-detail">
        {selected ? (
          <LikedDetail p={selected} />
        ) : (
          <div className="empty" style={{ margin: 'auto' }}>
            {tr('选择论文查看详情', 'Select a paper to view details')}
          </div>
        )}
      </div>
    </div>
  );
}
