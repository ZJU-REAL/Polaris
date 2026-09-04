import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Icon, type IconName } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { Segmented } from '../../components/ui/Segmented';
import { EmptyState } from '../../components/ui/EmptyState';
import { api, isLabScopedTask, type VoyageRead } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { useLibraries } from '../libraries/hooks';
import {
  FILTERS,
  KIND_META,
  LIBRARY_TASK_KINDS,
  SkeletonRows,
  VoyageRow,
  matchFilter,
  type Filter,
} from '../voyages/VoyagesPage';

/* ============================================================
   /lab — 课题外的任务列表（文献库任务与每日新论文，按归属分组）。

   原「实验室工作台」还有一个数据面板标签（索引统计 / AI 用量 / 跨库图谱），
   单人产品里「全实验室概况」没有受众，随 #626 整体移除（含后端 /lab/* 端点），
   这个路由只剩任务列表。课题任务归课题工作台的「任务」标签，这里不重复列
   （判据是 isLabScopedTask，与任务详情的面包屑/返回链接同一份）。
   ============================================================ */

type GroupKey = string;

interface VoyageGroup {
  key: GroupKey;
  /** 分组标题（已本地化） */
  title: string;
  /** 分组说明（已本地化） */
  hint: string;
  icon: IconName;
  items: VoyageRead[];
}

/** 可折叠分组：标题行 + 计数 + 行列表。 */
function TaskGroup({ group, open, onToggle }: { group: VoyageGroup; open: boolean; onToggle: () => void }) {
  return (
    <div className="card" style={{ overflow: 'hidden', marginBottom: 14 }}>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        style={{
          width: '100%',
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          padding: '12px 16px',
          background: 'transparent',
          border: 'none',
          cursor: 'pointer',
          textAlign: 'left',
          fontFamily: 'var(--sans)',
        }}
      >
        <Icon
          name="chevron"
          size={14}
          style={{ color: 'var(--text-3)', transform: open ? 'rotate(90deg)' : 'none', transition: 'transform .15s', flexShrink: 0 }}
        />
        <Icon name={group.icon} size={14} style={{ color: 'var(--text-3)', flexShrink: 0 }} />
        <span style={{ fontSize: 13, fontWeight: 650, color: 'var(--text)', letterSpacing: '-0.01em' }}>
          {group.title}
        </span>
        <span style={{ fontSize: 11.5, color: 'var(--text-4)', flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {group.hint}
        </span>
        <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-2)', flexShrink: 0 }}>
          {group.items.length}
        </span>
      </button>
      {open && (
        <div className="table-wrap" style={{ borderTop: '0.5px solid var(--border)' }}>
          {/* 归属名已在组标题上，行内不再重复 */}
          {group.items.map((v, i) => (
            <VoyageRow key={v.id} v={v} first={i === 0} />
          ))}
        </div>
      )}
    </div>
  );
}

export function LabPage() {
  const { data: libs } = useLibraries();

  const [filter, setFilter] = useState<Filter>('all');
  const [kindFilter, setKindFilter] = useState<string>('all');
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  // 不传 project_id = 我可见的全部任务；课题任务在下面按 isLabScopedTask 滤掉
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['voyages', 'all'],
    queryFn: () => api.listVoyages(),
    retry: false,
    refetchInterval: 30_000,
  });

  const libraryName = useMemo(() => new Map((libs ?? []).map((l) => [l.id, l.name] as const)), [libs]);

  const groups = useMemo<VoyageGroup[]>(() => {
    // 这里只列课题外的任务：文献库任务与每日新论文。课题任务归课题工作台，
    // 在两处都列会让人以为是两拨任务，也把这张表冲得很长。
    const rows = (data ?? []).filter(
      (v) =>
        isLabScopedTask(v) &&
        matchFilter(v, filter) &&
        (kindFilter === 'all' || v.kind === kindFilter),
    );
    const byLibrary = new Map<string, VoyageRead[]>();
    const daily: VoyageRead[] = [];
    const other: VoyageRead[] = [];
    for (const v of rows) {
      if (v.library_id) {
        const arr = byLibrary.get(v.library_id) ?? [];
        arr.push(v);
        byLibrary.set(v.library_id, arr);
      } else if (v.kind === 'daily_feed_sync') {
        // 每日新论文不挂任何课题或文献库，两个作用域 id 都为空 —— 自成一组，
        // 不能跟着「既不属课题也不属库」的兜底组走，那个标题词不达意。
        daily.push(v);
      } else {
        other.push(v);
      }
    }
    const newest = (list: VoyageRead[]) =>
      [...list].sort((a, b) => b.created_at.localeCompare(a.created_at));
    const out: VoyageGroup[] = [];
    for (const [lid, list] of byLibrary) {
      out.push({
        key: `library:${lid}`,
        title: libraryName.get(lid) ?? tr('未知文献库', 'Unknown library'),
        hint: tr('文献库任务', 'Library tasks'),
        icon: 'book',
        items: newest(list),
      });
    }
    if (daily.length > 0) {
      out.push({
        key: 'daily',
        title: tr('每日新论文', 'Daily papers'),
        hint: tr('全局共享，不属于任何文献库', 'Shared globally, not tied to a library'),
        icon: 'refresh',
        items: newest(daily),
      });
    }
    // 兜底组：正常不该有东西落进来（真有就是新类型没归位），宁可露出也不要静默吞掉
    if (other.length > 0) {
      out.push({
        key: 'other',
        title: tr('其它任务', 'Other tasks'),
        hint: tr('既不属于文献库也不属于每日新论文', 'Neither a library nor the daily feed'),
        icon: 'sparkle',
        items: newest(other),
      });
    }
    // 文献库组在前、每日新论文次之、兜底垫底；组内按任务数多的在前
    const rank = (g: VoyageGroup) => (g.key.startsWith('library:') ? 0 : g.key === 'daily' ? 1 : 2);
    return out.sort((a, b) => rank(a) - rank(b) || b.items.length - a.items.length);
  }, [data, filter, kindFilter, libraryName]);

  return (
    <div className="page fadeup">
      <PageHead
        eyebrow="Polaris · Tasks"
        title={tr('文献任务', 'Library Tasks')}
      />

      <div className="row gap10" style={{ marginBottom: 16, flexWrap: 'wrap' }}>
        <Segmented options={FILTERS.map((f) => ({ v: f.v, label: tr(f.zh, f.en) }))} value={filter} onChange={setFilter} />
        <select
          className="input"
          aria-label={tr('按类型筛选', 'Filter by type')}
          value={kindFilter}
          onChange={(e) => setKindFilter(e.target.value)}
          style={{ height: 33, fontSize: 12.5, fontWeight: 600, width: 128, color: kindFilter === 'all' ? 'var(--text-3)' : 'var(--text)' }}
        >
          <option value="all">{tr('全部类型', 'All types')}</option>
          {/* 只列课题外的类型（建库 / 增量更新 / 每日新论文）。课题类型不列：
              这张表里根本不会出现，列出来只会选中后永远空列表。 */}
          {LIBRARY_TASK_KINDS.map((k) => (
            <option key={k} value={k}>{tr(KIND_META[k]?.zh ?? k, KIND_META[k]?.en)}</option>
          ))}
        </select>
      </div>

      {isLoading ? (
        <SkeletonRows />
      ) : isError ? (
        <div className="card">
          <EmptyState
            icon="x"
            title={tr('无法加载任务列表', 'Failed to load tasks')}
            desc={tr('后端不可用或接口尚未就绪，稍后可重试。', 'Backend unavailable or endpoint not ready — try again later.')}
            compact
            action={
              <button className="btn btn-soft" onClick={() => void refetch()}>
                <Icon name="refresh" size={13} />
                {tr('重试', 'Retry')}
              </button>
            }
          />
        </div>
      ) : groups.length === 0 ? (
        <div className="card">
          <EmptyState
            icon="compass"
            title={tr('暂无任务', 'No tasks yet')}
            desc={
              filter !== 'all' || kindFilter !== 'all'
                ? tr('当前筛选条件下没有任务，换个筛选试试。', 'No tasks match the current filters — try different ones.')
                : tr(
                    '建库、增量更新与每日新论文的任务会出现在这里。想法生成、实验等课题任务在课题工作台看。',
                    'Library builds, incremental syncs and the daily paper feed show up here. Idea generation, experiments and other topic tasks live in the topic workbench.',
                  )
            }
            compact
          />
        </div>
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
