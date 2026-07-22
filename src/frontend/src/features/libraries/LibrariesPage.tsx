import { useNavigate } from 'react-router-dom';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { EmptyState } from '../../components/ui/EmptyState';
import { fmtTime } from '../../lib/format';
import type { DirectionLibrarySummary } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { useLibraries, libraryPath } from './hooks';

/* ============================================================
   /libraries — 文献库列表（实验室区，P5c）
   卡片流：库名 / 方向陈述 / 论文·概念数 / 最近更新；
   「我的课题的库」有标识；点击进 /libraries/:id 详情。
   ============================================================ */

function LibraryCard({ lib, onOpen }: { lib: DirectionLibrarySummary; onOpen: () => void }) {
  const updated = lib.last_compiled_at ?? lib.last_synced_at;
  return (
    <div
      className="card hoverable"
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onOpen();
        }
      }}
      style={{ padding: '18px 20px', display: 'flex', flexDirection: 'column', gap: 10, cursor: 'pointer' }}
    >
      <div className="row gap8" style={{ alignItems: 'flex-start' }}>
        <span
          style={{
            width: 34,
            height: 34,
            borderRadius: 10,
            background: 'var(--accent-soft)',
            color: 'var(--accent)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            flexShrink: 0,
          }}
        >
          <Icon name="book" size={17} />
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="row gap8">
            <span style={{ fontSize: 14.5, fontWeight: 680, lineHeight: 1.3 }} title={lib.name}>
              {lib.name}
            </span>
            {lib.is_mine && (
              <span className="pill sm" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)', flexShrink: 0 }}>
                {tr('我的课题', 'My topic')}
              </span>
            )}
          </div>
        </div>
        <Icon name="arrow" size={14} style={{ color: 'var(--text-4)', flexShrink: 0, marginTop: 4 }} />
      </div>
      <div
        style={{
          fontSize: 12.5,
          lineHeight: 1.55,
          color: 'var(--text-3)',
          display: '-webkit-box',
          WebkitLineClamp: 2,
          WebkitBoxOrient: 'vertical',
          overflow: 'hidden',
          minHeight: 38,
        }}
      >
        {lib.statement ?? tr('这个方向还没有写一句话介绍。', 'No statement for this direction yet.')}
      </div>
      <div className="row gap10" style={{ fontSize: 11.5, color: 'var(--text-3)' }}>
        <span className="row gap6">
          <Icon name="file" size={12} />
          {tr(`${lib.paper_count} 篇论文`, `${lib.paper_count} papers`)}
        </span>
        <span className="row gap6">
          <Icon name="layers" size={12} />
          {tr(`${lib.concept_count} 个概念`, `${lib.concept_count} concepts`)}
        </span>
        <span className="mono" style={{ marginLeft: 'auto', fontSize: 10.5, color: 'var(--text-4)' }}>
          {updated ? `${tr('更新于', 'Updated')} ${fmtTime(updated)}` : tr('还没有内容', 'Empty')}
        </span>
      </div>
    </div>
  );
}

export function LibrariesPage() {
  const navigate = useNavigate();
  const { data, isLoading, isError, refetch } = useLibraries();
  const libraries = data ?? [];
  // 我的课题的库排前面，其余按名称
  const sorted = [...libraries].sort(
    (a, b) => Number(b.is_mine) - Number(a.is_mine) || a.name.localeCompare(b.name),
  );

  return (
    <div className="page fadeup" style={{ maxWidth: 1200 }}>
      <PageHead
        eyebrow={tr('实验室', 'Lab')}
        title={tr('文献库', 'Libraries')}
        sub={tr(
          '按研究方向维护的公共文献库：解读、概念和原文对所有人开放，随便逛。',
          'Shared per-direction libraries — wikis, concepts and full texts are open to everyone.',
        )}
      />

      {isLoading ? (
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))',
            gap: 14,
          }}
        >
          {[0, 1, 2].map((i) => (
            <div key={i} className="skel" style={{ height: 150, borderRadius: 14 }} />
          ))}
        </div>
      ) : isError ? (
        <EmptyState
          icon="x"
          title={tr('无法加载文献库列表', 'Failed to load libraries')}
          desc={tr('后端不可用或接口尚未就绪。', 'Backend unavailable or API not ready.')}
          action={
            <button className="btn btn-soft sm" onClick={() => void refetch()}>
              {tr('重试', 'Retry')}
            </button>
          }
        />
      ) : sorted.length === 0 ? (
        <EmptyState
          icon="book"
          title={tr('还没有文献库', 'No libraries yet')}
          desc={tr(
            '创建课题后会自动生成对应方向的文献库；先去建一个课题吧。',
            'A direction library is created with each topic — create a topic first.',
          )}
          action={
            <button className="btn btn-primary sm" onClick={() => navigate('/projects/new')}>
              <Icon name="plus" size={13} />
              {tr('新建课题', 'New topic')}
            </button>
          }
        />
      ) : (
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))',
            gap: 14,
          }}
        >
          {sorted.map((lib) => (
            <LibraryCard key={lib.id} lib={lib} onOpen={() => navigate(libraryPath(lib.id))} />
          ))}
        </div>
      )}
    </div>
  );
}
