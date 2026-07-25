import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { Segmented } from '../../components/ui/Segmented';
import { StatCard } from '../../components/ui/StatCard';
import { EmptyState } from '../../components/ui/EmptyState';
import { useProject } from '../../app/project';
import { api, type DirectionLibrarySummary, type VoyageRead } from '../../lib/api';
import { fmtFullTime, fmtRelative } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { useLibraries, libraryPath } from '../libraries/hooks';
import {
  FILTERS,
  KIND_META,
  SkeletonRows,
  VoyageRow,
  matchFilter,
  type Filter,
} from '../voyages/VoyagesPage';

/* ============================================================
   /lab — 实验室工作台
   两个标签：
   - 概况：文献库与每日新论文的汇总（全部走既有列表接口，无新增聚合端点）
   - 任务：全部可见任务按归属分组（课题任务 / 文献库任务 / 其它），
     覆盖课题工作台看不到的「课题外任务」（VoyageRun.project_id 可为空）
   ============================================================ */

type LabTab = 'overview' | 'tasks';

/* ------------------------------------------------------------------ 概况 */

/** 库状态小标：待审批 / 已驳回；已激活不额外占位（默认态不加噪）。 */
function LibStatusPill({ status }: { status: DirectionLibrarySummary['status'] }) {
  if (status === 'active') return null;
  const cfg =
    status === 'pending'
      ? { zh: '待审批', en: 'Pending', bg: 'var(--warn-bg)', tx: 'var(--warn-tx)' }
      : { zh: '已驳回', en: 'Rejected', bg: 'var(--danger-bg)', tx: 'var(--danger-tx)' };
  return (
    <span className="pill sm" style={{ background: cfg.bg, color: cfg.tx, flexShrink: 0 }}>
      {tr(cfg.zh, cfg.en)}
    </span>
  );
}

/** 文献库汇总卡：公共/个人计数 + 每库一行（论文数 / 概念数 / 上次同步时间 / 状态）。 */
function LibrariesCard() {
  const navigate = useNavigate();
  const { data, isLoading, isError } = useLibraries();
  const libs = useMemo(() => {
    const list = data ?? [];
    // 公共库在前，其次论文多的在前
    return [...list].sort(
      (a, b) => Number(b.is_public) - Number(a.is_public) || b.paper_count - a.paper_count,
    );
  }, [data]);
  const publicCount = libs.filter((l) => l.is_public).length;
  const personalCount = libs.length - publicCount;
  const paperTotal = libs.reduce((acc, l) => acc + l.paper_count, 0);

  return (
    <div className="card" style={{ overflow: 'hidden', marginBottom: 20 }}>
      <div className="row gap10" style={{ padding: '14px 18px', borderBottom: '0.5px solid var(--border)' }}>
        <Icon name="book" size={15} style={{ color: 'var(--text-2)', flexShrink: 0 }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13.5, fontWeight: 650, letterSpacing: '-0.01em' }}>
            {tr('文献库', 'Libraries')}
          </div>
          <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 2 }}>
            {isLoading || isError
              ? tr('实验室共享的方向文献库', 'Shared direction libraries')
              : tr(
                  `公共库 ${publicCount} 个 · 个人库 ${personalCount} 个 · 共 ${paperTotal} 篇论文`,
                  `${publicCount} public · ${personalCount} personal · ${paperTotal} papers`,
                )}
          </div>
        </div>
        <Link className="btn btn-soft sm" to="/libraries">
          {tr('全部文献库', 'All libraries')}
          <Icon name="chevron" size={12} />
        </Link>
      </div>

      {isLoading ? (
        <div className="col gap10" style={{ padding: '16px 18px' }}>
          {[0, 1, 2].map((i) => (
            <div key={i} className="skel" style={{ height: 14, width: `${70 - i * 12}%` }} />
          ))}
        </div>
      ) : isError ? (
        <EmptyState
          icon="x"
          title={tr('无法加载文献库', 'Failed to load libraries')}
          desc={tr('后端不可用，稍后重试。', 'Backend unavailable — try again later.')}
          compact
        />
      ) : libs.length === 0 ? (
        <EmptyState
          icon="book"
          title={tr('还没有文献库', 'No libraries yet')}
          desc={tr('新建一个文献库后，建库与同步任务会出现在「任务」标签里。', 'Create a library — its build and sync tasks will show up under the Tasks tab.')}
          compact
          action={
            <Link className="btn btn-primary sm" to="/libraries">
              {tr('去建库', 'Create one')}
            </Link>
          }
        />
      ) : (
        <div className="table-wrap">
          {libs.map((l, i) => (
            <div
              key={l.id}
              className="voyage-row"
              onClick={() => navigate(libraryPath(l.id))}
              style={{ borderTop: i > 0 ? '0.5px solid var(--border)' : 'none' }}
            >
              <span
                className="pill sm"
                style={{
                  background: l.is_public ? 'var(--ok-bg)' : 'var(--violet-bg)',
                  color: l.is_public ? 'var(--ok-tx)' : 'var(--violet-tx)',
                  flexShrink: 0,
                }}
              >
                {l.is_public ? tr('公共', 'Public') : tr('个人', 'Personal')}
              </span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div
                  title={l.name}
                  style={{ fontSize: 13.5, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                >
                  {l.name}
                </div>
                <div className="row gap8" style={{ marginTop: 4 }}>
                  {l.owner_name && (
                    <span style={{ fontSize: 11, color: 'var(--text-3)' }}>{l.owner_name}</span>
                  )}
                  <span style={{ fontSize: 11, color: 'var(--text-3)' }} title={fmtFullTime(l.last_synced_at)}>
                    {l.last_synced_at
                      ? tr(`上次同步 ${fmtRelative(l.last_synced_at)}`, `synced ${fmtRelative(l.last_synced_at)}`)
                      : tr('还没同步过', 'never synced')}
                  </span>
                </div>
              </div>
              <span className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)', width: 96, flexShrink: 0, textAlign: 'right' }}>
                {l.paper_count} {tr('篇', 'papers')}
              </span>
              <span className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)', width: 96, flexShrink: 0, textAlign: 'right' }}>
                {l.concept_count} {tr('概念', 'concepts')}
              </span>
              <span style={{ width: 70, display: 'flex', justifyContent: 'flex-end', flexShrink: 0 }}>
                <LibStatusPill status={l.status} />
              </span>
              <Icon name="chevron" size={14} style={{ color: 'var(--text-4)', flexShrink: 0 }} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** 本地时区的 YYYY-MM-DD（每日新论文按日期字符串分桶）。 */
function todayKey(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** 每日新论文汇总卡：今日新增 / 近 7 天合计 + 最近 7 天迷你分布（单色柱，标签用文本色）。 */
function DailyCard() {
  const { data, isLoading, isError } = useQuery({
    // 与 /daily 页共享缓存
    queryKey: ['daily-days'],
    queryFn: () => api.listDailyDays(),
    retry: false,
    staleTime: 60_000,
  });

  const days = useMemo(() => [...(data ?? [])].sort((a, b) => a.date.localeCompare(b.date)).slice(-7), [data]);
  const today = todayKey();
  const todayCount = days.find((d) => d.date === today)?.count ?? 0;
  const weekTotal = days.reduce((acc, d) => acc + d.count, 0);
  const max = Math.max(1, ...days.map((d) => d.count));

  return (
    <div className="card" style={{ overflow: 'hidden' }}>
      <div className="row gap10" style={{ padding: '14px 18px', borderBottom: '0.5px solid var(--border)' }}>
        <Icon name="heart" size={15} style={{ color: 'var(--text-2)', flexShrink: 0 }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13.5, fontWeight: 650, letterSpacing: '-0.01em' }}>
            {tr('每日新论文', 'Daily Papers')}
          </div>
          <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 2 }}>
            {tr('arXiv 每日新提交，保留最近 7 天', 'New arXiv submissions, last 7 days kept')}
          </div>
        </div>
        <Link className="btn btn-soft sm" to="/daily">
          {tr('去浏览', 'Browse')}
          <Icon name="chevron" size={12} />
        </Link>
      </div>

      {isLoading ? (
        <div className="col gap10" style={{ padding: '18px' }}>
          <div className="skel" style={{ height: 14, width: '40%' }} />
          <div className="skel" style={{ height: 56, width: '100%' }} />
        </div>
      ) : isError ? (
        <EmptyState
          icon="x"
          title={tr('无法加载每日新论文', 'Failed to load daily papers')}
          desc={tr('后端不可用，稍后重试。', 'Backend unavailable — try again later.')}
          compact
        />
      ) : days.length === 0 ? (
        <EmptyState
          icon="heart"
          title={tr('还没有抓取到新论文', 'No new papers yet')}
          desc={tr('每日抓取任务跑过之后，这里会显示每天的新增量。', 'Counts show up here once the daily fetch has run.')}
          compact
        />
      ) : (
        <div style={{ padding: '16px 18px' }}>
          <div className="row gap20" style={{ marginBottom: 16, flexWrap: 'wrap' }}>
            <div>
              <div className="mono" style={{ fontSize: 24, fontWeight: 700, letterSpacing: '-0.02em' }}>{todayCount}</div>
              <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 2 }}>{tr('今天新增', 'New today')}</div>
            </div>
            <div>
              <div className="mono" style={{ fontSize: 24, fontWeight: 700, letterSpacing: '-0.02em' }}>{weekTotal}</div>
              <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 2 }}>{tr('近 7 天合计', 'Last 7 days')}</div>
            </div>
          </div>

          {/*
            单序列迷你分布：柱子统一 accent 色（明度区分靠高度），标签用文本色。
            /daily 目前不认 ?date=，柱子统一进列表页（日期在那里再选）。
          */}
          <div className="row" style={{ gap: 8, alignItems: 'flex-end' }}>
            {days.map((d) => (
              <Link
                key={d.date}
                to="/daily"
                title={`${d.date} · ${d.count}`}
                style={{ flex: 1, minWidth: 0, textDecoration: 'none', display: 'block' }}
              >
                <div style={{ height: 52, display: 'flex', alignItems: 'flex-end' }}>
                  <div
                    style={{
                      width: '100%',
                      height: `${Math.max(3, Math.round((d.count / max) * 52))}px`,
                      borderRadius: '4px 4px 2px 2px',
                      background: d.date === today ? 'var(--accent)' : 'var(--accent-soft)',
                      border: d.date === today ? 'none' : '0.5px solid var(--border-2)',
                    }}
                  />
                </div>
                <div
                  className="mono"
                  style={{ fontSize: 10, color: 'var(--text-3)', textAlign: 'center', marginTop: 5 }}
                >
                  {d.count}
                </div>
                <div
                  className="mono"
                  style={{ fontSize: 9.5, color: 'var(--text-4)', textAlign: 'center', marginTop: 1 }}
                >
                  {d.date.slice(5)}
                </div>
              </Link>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function OverviewTab() {
  const { data: libs } = useLibraries();
  const { data: days } = useQuery({
    queryKey: ['daily-days'],
    queryFn: () => api.listDailyDays(),
    retry: false,
    staleTime: 60_000,
  });
  const { data: voyages } = useQuery({
    queryKey: ['voyages', 'all'],
    queryFn: () => api.listVoyages(),
    retry: false,
    refetchInterval: 30_000,
  });

  const libList = libs ?? [];
  const dayList = days ?? [];
  const activeVoyages = (voyages ?? []).filter((v) => matchFilter(v, 'active') || matchFilter(v, 'paused')).length;

  return (
    <>
      <div className="row gap16 dash-stats" style={{ marginBottom: 20 }}>
        <StatCard
          icon="book"
          label="文献库"
          en="Libraries"
          value={libs ? libList.length : '—'}
          sub={libs ? tr(`${libList.filter((l) => l.is_public).length} 个公共库`, `${libList.filter((l) => l.is_public).length} public`) : undefined}
        />
        <StatCard
          icon="file"
          label="论文总量"
          en="Papers"
          value={libs ? libList.reduce((a, l) => a + l.paper_count, 0) : '—'}
          sub={tr('全部文献库', 'across libraries')}
        />
        <StatCard
          icon="heart"
          label="每日新论文"
          en="Daily papers"
          value={days ? dayList.reduce((a, d) => a + d.count, 0) : '—'}
          sub={tr('近 7 天', 'last 7 days')}
        />
        <StatCard
          icon="compass"
          label="进行中的任务"
          en="Running tasks"
          value={voyages ? activeVoyages : '—'}
          sub={tr('含等待审批', 'incl. waiting')}
          accent
        />
      </div>

      <LibrariesCard />
      <DailyCard />
    </>
  );
}

/* ------------------------------------------------------------------ 任务 */

type GroupKey = string;

interface VoyageGroup {
  key: GroupKey;
  /** 分组标题（已本地化） */
  title: string;
  /** 分组说明（已本地化） */
  hint: string;
  icon: 'dashboard' | 'book' | 'sparkle';
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

function TasksTab() {
  const { projects } = useProject();
  const { data: libs } = useLibraries();

  const [filter, setFilter] = useState<Filter>('all');
  const [kindFilter, setKindFilter] = useState<string>('all');
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  // 不传 project_id = 我可见的全部任务（含 project_id 为空的课题外任务）
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['voyages', 'all'],
    queryFn: () => api.listVoyages(),
    retry: false,
    refetchInterval: 30_000,
  });

  const projectName = useMemo(() => new Map(projects.map((p) => [p.id, p.name] as const)), [projects]);
  const libraryName = useMemo(() => new Map((libs ?? []).map((l) => [l.id, l.name] as const)), [libs]);

  const groups = useMemo<VoyageGroup[]>(() => {
    const rows = (data ?? []).filter(
      (v) => matchFilter(v, filter) && (kindFilter === 'all' || v.kind === kindFilter),
    );
    const byProject = new Map<string, VoyageRead[]>();
    const byLibrary = new Map<string, VoyageRead[]>();
    const other: VoyageRead[] = [];
    for (const v of rows) {
      if (v.project_id) {
        const arr = byProject.get(v.project_id) ?? [];
        arr.push(v);
        byProject.set(v.project_id, arr);
      } else if (v.library_id) {
        const arr = byLibrary.get(v.library_id) ?? [];
        arr.push(v);
        byLibrary.set(v.library_id, arr);
      } else {
        other.push(v);
      }
    }
    const newest = (list: VoyageRead[]) =>
      [...list].sort((a, b) => b.created_at.localeCompare(a.created_at));
    const out: VoyageGroup[] = [];
    for (const [pid, list] of byProject) {
      out.push({
        key: `project:${pid}`,
        title: projectName.get(pid) ?? tr('未知课题', 'Unknown topic'),
        hint: tr('课题任务', 'Topic tasks'),
        icon: 'dashboard',
        items: newest(list),
      });
    }
    for (const [lid, list] of byLibrary) {
      out.push({
        key: `library:${lid}`,
        title: libraryName.get(lid) ?? tr('未知文献库', 'Unknown library'),
        hint: tr('文献库任务（课题外）', 'Library tasks (outside topics)'),
        icon: 'book',
        items: newest(list),
      });
    }
    if (other.length > 0) {
      out.push({
        key: 'other',
        title: tr('其它任务', 'Other tasks'),
        hint: tr('既不属于课题也不属于文献库', 'Neither topic nor library'),
        icon: 'sparkle',
        items: newest(other),
      });
    }
    // 课题组在前、文献库组次之、其它垫底；组内按任务数多的在前
    const rank = (g: VoyageGroup) => (g.key.startsWith('project:') ? 0 : g.key.startsWith('library:') ? 1 : 2);
    return out.sort((a, b) => rank(a) - rank(b) || b.items.length - a.items.length);
  }, [data, filter, kindFilter, projectName, libraryName]);

  return (
    <>
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
          {Object.entries(KIND_META).map(([k, m]) => (
            <option key={k} value={k}>{tr(m.zh, m.en)}</option>
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
                : tr('还没有任务。发起建库、想法生成、实验等操作后，任务会出现在这里。', 'No tasks yet. Tasks show up here once you start ingest, idea generation, experiments, and so on.')
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
    </>
  );
}

/* ------------------------------------------------------------------ 页面 */

export function LabPage() {
  // ?tab= 深链进入后清参数（与课题工作台一致）
  const [tab, setTab] = useState<LabTab>('overview');
  const [searchParams, setSearchParams] = useSearchParams();
  useEffect(() => {
    const tabParam = searchParams.get('tab');
    if (!tabParam) return;
    if ((['overview', 'tasks'] as const).some((t) => t === tabParam)) setTab(tabParam as LabTab);
    setSearchParams({}, { replace: true });
  }, [searchParams, setSearchParams]);

  return (
    <div className="page fadeup">
      <PageHead
        eyebrow="Polaris · Lab"
        title={tr('实验室工作台', 'Lab Workbench')}
        sub={tr('实验室共享资源的总览，以及不属于任何课题的任务。', 'An overview of shared lab resources, plus tasks that belong to no topic.')}
      />

      <div style={{ marginBottom: 22 }}>
        <Segmented
          options={[
            { v: 'overview' as const, label: tr('概况', 'Overview') },
            { v: 'tasks' as const, label: tr('任务', 'Tasks') },
          ]}
          value={tab}
          onChange={setTab}
        />
      </div>

      {tab === 'overview' ? <OverviewTab /> : <TasksTab />}
    </div>
  );
}
