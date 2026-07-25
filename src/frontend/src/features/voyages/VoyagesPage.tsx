import { useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Icon, type IconName } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { StatusPill } from '../../components/ui/StatusPill';
import { Segmented } from '../../components/ui/Segmented';
import { EmptyState } from '../../components/ui/EmptyState';
import { useProject } from '../../app/project';
import { api, VOYAGE_TERMINAL, type VoyageRead } from '../../lib/api';
import { fmtDuration, fmtFullTime, fmtRelative } from '../../lib/format';
import { tr } from '../../lib/i18n';

/* ============================================================
   /voyages — AI 任务列表：状态/类型过滤 + 行（类型徽章/目标/
   迷你进度条/耗时/状态）。任务由建库/想法/实验等业务动作发起。
   ============================================================ */

export type Filter = 'all' | 'active' | 'paused' | 'done' | 'failed';

export const FILTERS: { v: Filter; zh: string; en: string }[] = [
  { v: 'all', zh: '全部', en: 'All' },
  { v: 'active', zh: '进行中', en: 'Active' },
  { v: 'paused', zh: '等待中', en: 'Waiting' },
  { v: 'done', zh: '已完成', en: 'Done' },
  { v: 'failed', zh: '失败/取消', en: 'Failed/cancelled' },
];

/** 正在推进中的状态（列表行左缘蓝条 + 淡蓝底 + 进度条动画）。 */
const RUNNING_STATUSES: ReadonlySet<string> = new Set(['planning', 'executing', 'verifying', 'replanning']);

export function matchFilter(v: VoyageRead, f: Filter): boolean {
  switch (f) {
    case 'all':
      return true;
    case 'active':
      return RUNNING_STATUSES.has(v.status);
    case 'paused':
      return v.status === 'paused_gate' || v.status === 'paused_error';
    case 'done':
      return v.status === 'done';
    case 'failed':
      return v.status === 'failed' || v.status === 'cancelled';
  }
}

// —— 任务类型：中文标签 + 图标 + 低饱和语义底色（全部走 token） ——
interface KindMeta {
  zh: string;
  en?: string;
  icon: IconName;
  bg: string;
  tx: string;
}

export const KIND_META: Record<string, KindMeta> = {
  wiki_bootstrap: { zh: '初始建库', en: 'Initial library build', icon: 'book', bg: 'var(--info-bg)', tx: 'var(--info-tx)' },
  wiki_ingest: { zh: '增量更新', en: 'Incremental sync', icon: 'refresh', bg: 'var(--accent-soft)', tx: 'var(--accent-text)' },
  idea_forge: { zh: '想法生成', en: 'Idea forge', icon: 'bulb', bg: 'var(--warn-bg)', tx: 'var(--warn-tx)' },
  idea_review: { zh: '评审锦标赛', en: 'Review tournament', icon: 'scale', bg: 'var(--violet-bg)', tx: 'var(--violet-tx)' },
  idea_proposal: { zh: '方案细化', en: 'Proposal', icon: 'bulb', bg: 'var(--warn-bg)', tx: 'var(--warn-tx)' },
  experiment: { zh: '实验', en: 'Experiment', icon: 'flask', bg: 'var(--ok-bg)', tx: 'var(--ok-tx)' },
  paper_writing: { zh: '论文起草', en: 'Draft writing', icon: 'pen', bg: 'var(--violet-bg)', tx: 'var(--violet-tx)' },
  paper_review: { zh: '论文评审', en: 'Paper review', icon: 'check', bg: 'var(--violet-bg)', tx: 'var(--violet-tx)' },
  presentation: { zh: '论文分享', en: 'Paper slides', icon: 'chart', bg: 'var(--info-bg)', tx: 'var(--info-tx)' },
  custom: { zh: '流程技能', en: 'Workflow skill', icon: 'sparkle', bg: 'var(--accent-soft)', tx: 'var(--accent-text)' },
  demo: { zh: '演示', en: 'Demo', icon: 'play', bg: 'var(--surface-3)', tx: 'var(--text-2)' },
  daily_feed_sync: { zh: '每日新论文', en: 'Daily paper sync', icon: 'refresh', bg: 'var(--info-bg)', tx: 'var(--info-tx)' },
};

function kindMeta(kind: string): KindMeta {
  return KIND_META[kind] ?? { zh: kind, icon: 'sparkle', bg: 'var(--surface-3)', tx: 'var(--text-2)' };
}

/* —— 任务层级：与后端 app/models/voyage.py 的 LIBRARY_KINDS 对应 ——
   文献库任务（建库/增量更新）与每日新论文任务归实验室工作台，其余归课题。
   课题工作台的类型筛选只列课题类型：那里根本不会出现库任务，列出来只会
   选中后永远空列表。 */
export const LIBRARY_TASK_KINDS = ['wiki_bootstrap', 'wiki_ingest', 'daily_feed_sync'] as const;
export const TOPIC_TASK_KINDS = Object.keys(KIND_META).filter(
  (k) => !(LIBRARY_TASK_KINDS as readonly string[]).includes(k),
);

/** 类型徽章：图标 + 中文标签，低饱和语义底色。 */
export function KindBadge({ kind }: { kind: string }) {
  const m = kindMeta(kind);
  return (
    <span className="pill sm" style={{ background: m.bg, color: m.tx, flexShrink: 0 }} title={kind}>
      <Icon name={m.icon} size={11} sw={1.9} />
      {tr(m.zh, m.en)}
    </span>
  );
}

/** 迷你步骤进度条：~90px 细条 + "3/7" 等宽小字。 */
function StepProgress({ v }: { v: VoyageRead }) {
  const total = Array.isArray(v.plan) ? v.plan.length : 0;
  if (!total) {
    return (
      <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }} title={tr('尚未生成执行计划', 'No execution plan yet')}>
        —
      </span>
    );
  }
  const cur = Math.min((v.cursor ?? 0) + 1, total);
  const done = v.status === 'done';
  const failedLike = v.status === 'failed' || v.status === 'paused_error';
  const cancelled = v.status === 'cancelled';
  const running = RUNNING_STATUSES.has(v.status);
  const frac = done ? 1 : cur / total;
  const fill = done
    ? 'var(--ok)'
    : failedLike
      ? 'var(--danger)'
      : cancelled
        ? 'var(--muted-dot)'
        : 'var(--accent)';
  return (
    <span className="row gap8" title={done ? tr(`全部 ${total} 步已完成`, `All ${total} steps done`) : tr(`第 ${cur} 步，共 ${total} 步`, `Step ${cur} of ${total}`)}>
      <span className="mini-bar">
        <i className={running ? 'anim' : undefined} style={{ width: `${frac * 100}%`, background: fill }} />
      </span>
      <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)', minWidth: 30, textAlign: 'right' }}>
        {cur}/{total}
      </span>
    </span>
  );
}

/**
 * 任务列表的一行：类型徽章 / 目标 + id + 相对时间 / 进度 / 耗时 / 状态。
 * 课题工作台「任务」标签与实验室工作台「任务」标签共用（外层各自套 .table-wrap）。
 */
export function VoyageRow({ v, first }: { v: VoyageRead; first?: boolean }) {
  const navigate = useNavigate();
  const active = !VOYAGE_TERMINAL.has(v.status);
  const running = RUNNING_STATUSES.has(v.status);
  return (
    <div
      className={`voyage-row${running ? ' running' : ''}`}
      onClick={() => navigate(`/voyages/${v.id}`)}
      style={{ borderTop: first ? 'none' : '0.5px solid var(--border)' }}
    >
      <KindBadge kind={v.kind} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          title={v.goal}
          style={{ fontSize: 13.5, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
        >
          {v.goal}
        </div>
        <div className="row gap8" style={{ marginTop: 4 }}>
          <span className="mono" style={{ fontSize: 10, color: 'var(--text-4)' }}>{v.id.slice(0, 8)}</span>
          <span style={{ fontSize: 11, color: 'var(--text-3)' }} title={fmtFullTime(v.created_at)}>
            · {fmtRelative(v.created_at)}
          </span>
        </div>
      </div>
      <span style={{ width: 130, display: 'flex', justifyContent: 'flex-end', flexShrink: 0 }}>
        <StepProgress v={v} />
      </span>
      <span
        className="mono"
        style={{
          fontSize: 11.5, color: 'var(--text-3)', width: 78, flexShrink: 0,
          display: 'inline-flex', alignItems: 'center', justifyContent: 'flex-end', gap: 4,
        }}
      >
        <Icon name="clock" size={11} />
        {fmtDuration(v.created_at, active ? null : v.updated_at)}
      </span>
      <span style={{ width: 134, display: 'flex', justifyContent: 'flex-end', flexShrink: 0 }}>
        <StatusPill status={v.status} sm />
      </span>
      <Icon name="chevron" size={14} style={{ color: 'var(--text-4)', flexShrink: 0 }} />
    </div>
  );
}

/** 加载骨架：shimmer 行，占位结构与真实行一致。 */
export function SkeletonRows() {
  return (
    <div className="card" style={{ overflow: 'hidden' }}>
      {[0, 1, 2, 3].map((i) => (
        <div
          key={i}
          className="row gap12"
          style={{ padding: '15px 18px', borderTop: i > 0 ? '0.5px solid var(--border)' : 'none' }}
        >
          <span className="skel" style={{ width: 76, height: 19 }} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="skel" style={{ width: '44%', height: 12, marginBottom: 7 }} />
            <div className="skel" style={{ width: 148, height: 9 }} />
          </div>
          <span className="skel" style={{ width: 126, height: 8, flexShrink: 0 }} />
          <span className="skel" style={{ width: 62, height: 10, flexShrink: 0 }} />
          <span className="skel" style={{ width: 88, height: 19, borderRadius: 10, flexShrink: 0 }} />
        </div>
      ))}
    </div>
  );
}

/**
 * 任务列表主体（过滤条 + 列表）：无自身 PageHead / 页壳，供工作台「任务」标签内嵌。
 * - `showScopeSwitch`：是否显示「当前课题 / 全部课题」切换器。课题工作台里固定看本课题
 *   （课题外的任务在 /lab 的「任务」标签按类型分组看），所以默认关闭。
 * - `labLink`：底部是否显示「课题外的任务在实验室工作台」引导（课题工作台用）。
 */
export function VoyagesList({
  showScopeSwitch = false,
  labLink = false,
  kinds,
}: {
  showScopeSwitch?: boolean;
  labLink?: boolean;
  /** 类型筛选下拉列哪些类型；不传=全部。课题工作台传课题类型，实验室传库/每日类型。 */
  kinds?: readonly string[];
} = {}) {
  const { currentProjectId } = useProject();

  const [filter, setFilter] = useState<Filter>('all');
  const [kindFilter, setKindFilter] = useState<string>('all');
  const [scope, setScope] = useState<'current' | 'all'>('current');

  const projectId = scope === 'current' ? (currentProjectId ?? undefined) : undefined;
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['voyages', projectId ?? 'all'],
    queryFn: () => api.listVoyages(projectId),
    retry: false,
    refetchInterval: 30_000,
  });
  const voyages = useMemo(
    () => (data ?? []).filter((v) => matchFilter(v, filter) && (kindFilter === 'all' || v.kind === kindFilter)),
    [data, filter, kindFilter],
  );

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
              {(kinds ?? Object.keys(KIND_META)).map((k) => (
                <option key={k} value={k}>{tr(KIND_META[k]?.zh ?? k, KIND_META[k]?.en)}</option>
              ))}
            </select>
            {showScopeSwitch && (
              <>
                <div style={{ flex: 1 }} />
                <Segmented
                  options={[
                    { v: 'current' as const, label: tr('当前课题', 'Current topic') },
                    { v: 'all' as const, label: tr('全部课题', 'All topics') },
                  ]}
                  value={scope}
                  onChange={setScope}
                />
              </>
            )}
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
          ) : voyages.length === 0 ? (
            <div className="card">
              <EmptyState
                icon="compass"
                title={tr('暂无任务', 'No tasks yet')}
                desc={
                  filter !== 'all' || kindFilter !== 'all'
                    ? tr('当前筛选条件下没有任务，换个筛选试试。', 'No tasks match the current filters — try different ones.')
                    : tr('还没有任务。想法生成、实验、论文起草等操作发起后，任务会出现在这里。', 'No tasks yet. Tasks show up here once you start idea generation, experiments, draft writing, and so on.')
                }
                compact
              />
            </div>
          ) : (
            <div className="card" style={{ overflow: 'hidden' }}>
              {/* 行内定宽列合计放不下窄屏，整块横滚而不是把中间的标题列挤成 0 宽 */}
              <div className="table-wrap">
                {voyages.map((v, i) => (
                  <VoyageRow key={v.id} v={v} first={i === 0} />
                ))}
              </div>
            </div>
          )}

          {labLink && (
            <div className="row gap6" style={{ marginTop: 12, fontSize: 12, color: 'var(--text-3)' }}>
              <Icon name="flask" size={13} style={{ flexShrink: 0 }} />
              <span>{tr('这里只看当前课题的任务；课题外的任务（建库、每日新论文等）在', 'Only this topic’s tasks are listed here. Tasks outside topics (library builds, daily papers, …) live in the')}</span>
              <Link to="/lab?tab=tasks" style={{ color: 'var(--accent)', fontWeight: 600 }}>
                {tr('实验室工作台', 'Lab Workbench')}
              </Link>
            </div>
          )}
    </>
  );
}

export function VoyagesPage() {
  return (
    <div className="page fadeup">
      <PageHead
        eyebrow="Polaris · Voyages"
        title={tr('任务', 'Tasks')}
        sub={tr('需要人工审批时任务会自动暂停，审批通过后继续执行。', 'Tasks pause automatically when they need approval, then resume once approved.')}
      />
      <VoyagesList showScopeSwitch />
    </div>
  );
}
