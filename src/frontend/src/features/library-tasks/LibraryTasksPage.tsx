import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Segmented } from '../../components/ui/Segmented';
import { EmptyState, ErrorState } from '../../components/ui/EmptyState';
import { topicPath } from '../../app/project';
import { api, type VoyageRead } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { useDirections } from '../deepresearch/directions';
import { useLibraries } from '../libraries/hooks';
import { FILTERS, SkeletonRows, VoyageRow, matchFilter, type Filter } from '../voyages/VoyagesPage';

/* ============================================================
   /activity — 全部任务，按归属分组：每个课题一组、每个文献库一组、每日新论文一组。

   顶栏「任务与待办」面板只列等你处理的、正在跑的和最近几条；要翻全部任务来这里。
   取代原来的「文献任务」页（/lab，只列课题外的任务）——同一个人的任务不必分两处看。
   一个课题自己的任务也在课题页头的「任务」页签里。
   ============================================================ */

interface VoyageGroup {
  key: string;
  /** 分组标题（已本地化） */
  title: string;
  /** 标题可以点过去的地方（课题 / 文献库） */
  href?: string;
  items: VoyageRead[];
}

/** 可折叠分组：标题行 + 计数 + 行列表。 */
function TaskGroup({ group, open, onToggle }: { group: VoyageGroup; open: boolean; onToggle: () => void }) {
  return (
    <div className="card" style={{ overflow: 'hidden', marginBottom: 14 }}>
      <div className="row gap8" style={{ padding: '4px 14px 4px 6px' }}>
        <button
          type="button"
          className="btn btn-plain sm"
          onClick={onToggle}
          aria-expanded={open}
          style={{ minWidth: 0, flex: 1, justifyContent: 'flex-start' }}
        >
          <Icon
            name="chevron"
            size={12}
            style={{ color: 'var(--text-3)', transform: open ? 'rotate(90deg)' : 'none', transition: 'transform .15s', flexShrink: 0 }}
          />
          <span style={{ fontWeight: 600, color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {group.title}
          </span>
          <span style={{ color: 'var(--text-3)', fontWeight: 400, flexShrink: 0 }}>{group.items.length}</span>
        </button>
        {group.href && (
          <Link className="btn btn-plain xs" to={group.href} style={{ textDecoration: 'none', flexShrink: 0 }}>
            {tr('打开', 'Open')}
          </Link>
        )}
      </div>
      {open && (
        <div className="table-wrap" style={{ borderTop: '0.5px solid var(--border)' }}>
          {group.items.map((v, i) => (
            <VoyageRow key={v.id} v={v} first={i === 0} />
          ))}
        </div>
      )}
    </div>
  );
}

export function ActivityPage() {
  const { data: libs } = useLibraries();
  const { byProject, byLibrary } = useDirections();

  const [filter, setFilter] = useState<Filter>('all');
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['voyages', 'all'],
    queryFn: () => api.listVoyages(),
    retry: false,
    refetchInterval: 30_000,
  });

  const groups = useMemo<VoyageGroup[]>(() => {
    const libraryName = new Map((libs ?? []).map((l) => [l.id, l.name] as const));
    const rows = (data ?? []).filter((v) => matchFilter(v, filter));
    const byKey = new Map<string, VoyageGroup>();
    const put = (key: string, title: string, href: string | undefined, v: VoyageRead) => {
      const g = byKey.get(key) ?? { key, title, href, items: [] };
      g.items.push(v);
      byKey.set(key, g);
    };
    for (const v of rows) {
      // 归属：先看它是不是某个课题的研究用的文献库，再看任务自己挂的课题，最后是文献库 / 每日新论文
      const topic = byLibrary(v.library_id) ?? byProject(v.project_id);
      if (topic) put(`topic:${topic.projectId}`, topic.name, topicPath(topic.projectId, 'tasks'), v);
      else if (v.library_id)
        put(`library:${v.library_id}`, libraryName.get(v.library_id) ?? tr('文献库', 'Library'), `/libraries/${v.library_id}`, v);
      else if (v.kind === 'daily_feed_sync') put('daily', tr('每日新论文', 'Daily papers'), '/daily', v);
      else put('other', tr('其他任务', 'Other tasks'), undefined, v);
    }
    const out = [...byKey.values()];
    for (const g of out) g.items.sort((a, b) => b.created_at.localeCompare(a.created_at));
    // 最近有动静的组在前
    const latest = (g: VoyageGroup) => g.items.reduce((m, v) => (v.updated_at > m ? v.updated_at : m), '');
    return out.sort((a, b) => latest(b).localeCompare(latest(a)));
  }, [data, filter, libs, byProject, byLibrary]);

  return (
    <div className="page fadeup" style={{ maxWidth: 980 }}>
      <div className="row gap10" style={{ marginBottom: 16, flexWrap: 'wrap' }}>
        <Segmented options={FILTERS.map((f) => ({ v: f.v, label: tr(f.zh, f.en) }))} value={filter} onChange={setFilter} />
      </div>

      {isLoading ? (
        <SkeletonRows />
      ) : isError ? (
        <ErrorState
          title={tr('无法加载任务，请确认本机引擎正在运行', 'Couldn’t load tasks. Check that the local engine is running.')}
          onRetry={() => void refetch()}
        />
      ) : groups.length === 0 ? (
        <EmptyState
          icon="compass"
          title={
            filter !== 'all'
              ? tr('没有符合筛选的任务', 'No tasks match the filter')
              : tr('还没有任务。开始一次研究后，它的每一步都会列在这里', 'No tasks yet. Once you start research, each step shows up here.')
          }
        />
      ) : (
        groups.map((g) => (
          <TaskGroup
            key={g.key}
            group={g}
            open={!collapsed[g.key]}
            onToggle={() => setCollapsed((c) => ({ ...c, [g.key]: !c[g.key] }))}
          />
        ))
      )}
    </div>
  );
}
