import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Drawer } from '../components/ui/Drawer';
import { ErrorState, LoadingState } from '../components/ui/EmptyState';
import { GateCard } from '../components/ui/GateCard';
import { Icon } from '../components/ui/Icon';
import { useDirections, type Direction } from '../features/deepresearch/directions';
import { runHeadline, runReason, waitingFor } from '../features/deepresearch/phase';
import { StatusDot, TaskStatus } from '../features/voyages/shared/StatusDot';
import { taskTitle } from '../features/voyages/shared/taskText';
import { api, VOYAGE_TERMINAL, type GateDecision, type GateRead, type VoyageRead } from '../lib/api';
import { fmtRelative } from '../lib/format';
import { tr } from '../lib/i18n';
import './ActivityDrawer.css';

/* ============================================================
   任务与待办：顶栏的一个入口，回答两个问题——
     · 有什么在等我？（要选定想法的课题、AI 的提问、出错停下的任务、审批）
     · 现在在跑什么？最近跑完了什么？
   取代原来分散的几处：审批铃铛、课题的「任务」标签、「文献任务」页的入口。
   完整的任务列表在 /activity。
   ============================================================ */

const RUNNING = new Set(['planning', 'executing', 'verifying', 'replanning']);
const RECENT_SHOWN = 5;

export interface ActivityData {
  /** 等你处理的课题（选定想法 / 回答提问 / 卡住） */
  waitingDirections: Direction[];
  /** 等你处理的任务（提问 / 出错停下、不会再自动重试） */
  waitingTasks: VoyageRead[];
  runningDirections: Direction[];
  runningTasks: VoyageRead[];
  recentTasks: VoyageRead[];
}

/**
 * 把课题和任务归到「等你处理 / 正在运行 / 最近完成」。
 * 一次研究的文献检索、寻找假设各是一个任务；课题已经在列表里时，它名下的这两类任务
 * 不再单列一行——同一件事不说两遍。
 */
export function groupActivity(directions: Direction[], voyages: VoyageRead[]): ActivityData {
  const waitingDirections = directions.filter((d) => d.run && waitingFor(d.run));
  const runningDirections = directions.filter((d) => d.run && d.run.status === 'running');
  const liveRuns = [...waitingDirections, ...runningDirections].flatMap((d) => (d.run ? [d.run] : []));
  const coveredByRun = (v: VoyageRead) =>
    liveRuns.some(
      (r) =>
        (v.kind === 'discovery' && v.project_id === r.project_id) ||
        ((v.kind === 'wiki_bootstrap' || v.kind === 'wiki_ingest') && !!v.library_id && v.library_id === r.library_id),
    );
  const newest = (a: VoyageRead, b: VoyageRead) => b.updated_at.localeCompare(a.updated_at);
  const open = voyages.filter((v) => !coveredByRun(v));
  return {
    waitingDirections,
    waitingTasks: open
      .filter((v) => v.status === 'paused_ask' || v.status === 'paused_gate' || (v.status === 'paused_error' && !v.retry_at))
      .sort(newest),
    runningDirections,
    runningTasks: open.filter((v) => RUNNING.has(v.status) || (v.status === 'paused_error' && !!v.retry_at)).sort(newest),
    recentTasks: voyages
      .filter((v) => VOYAGE_TERMINAL.has(v.status))
      .sort(newest)
      .slice(0, RECENT_SHOWN),
  };
}

/** 等你处理的总数：顶栏按钮上的数字。 */
export function waitingCount(data: ActivityData, pendingGates: number): number {
  return data.waitingDirections.length + data.waitingTasks.length + pendingGates;
}

export function useActivity(enabled = true) {
  const { directions } = useDirections();
  const voyagesQuery = useQuery({
    queryKey: ['voyages', 'all'],
    queryFn: () => api.listVoyages(),
    retry: false,
    refetchInterval: 30_000,
    enabled,
  });
  const data = useMemo(() => groupActivity(directions, voyagesQuery.data ?? []), [directions, voyagesQuery.data]);
  return { data, voyagesQuery };
}

function DirectionRow({ d, onNavigate }: { d: Direction; onNavigate: () => void }) {
  if (!d.run) return null;
  const head = runHeadline(d.run);
  const reason = runReason(d.run);
  return (
    <Link to={d.href} className="act-row" onClick={onNavigate}>
      <div className="act-row-title" title={d.name}>
        {d.name}
      </div>
      <div className="act-row-meta">
        <StatusDot tone={head.tone} label={head.label} />
        {reason && (
          <span className="act-row-reason" title={reason}>
            {reason}
          </span>
        )}
      </div>
    </Link>
  );
}

function TaskRow({ v, topicName, onNavigate }: { v: VoyageRead; topicName?: string; onNavigate: () => void }) {
  const title = taskTitle(v);
  return (
    <Link to={`/voyages/${v.id}`} className="act-row" onClick={onNavigate}>
      <div className="act-row-title" title={title}>
        {title}
      </div>
      <div className="act-row-meta">
        <TaskStatus status={v.status} />
        <span className="act-row-reason">
          {[topicName, fmtRelative(v.updated_at)].filter(Boolean).join(' · ')}
        </span>
      </div>
    </Link>
  );
}

export interface ActivityDrawerProps {
  open: boolean;
  onClose: () => void;
  pendingGates: GateRead[];
  gatesError: boolean;
  onRetryGates: () => void;
  expandedGate: string | null;
  onToggleGate: (id: string) => void;
  onDecide: (id: string, decision: GateDecision, comment?: string) => void;
  deciding: boolean;
}

export function ActivityDrawer({
  open,
  onClose,
  pendingGates,
  gatesError,
  onRetryGates,
  expandedGate,
  onToggleGate,
  onDecide,
  deciding,
}: ActivityDrawerProps) {
  const { data, voyagesQuery } = useActivity(open);
  const { byProject } = useDirections();
  const [showDecided, setShowDecided] = useState(false);
  const decidedQuery = useQuery({
    queryKey: ['gates', 'decided'],
    queryFn: () => api.listGates('decided'),
    retry: false,
    enabled: open && showDecided,
  });
  const decided = decidedQuery.data ?? [];

  const waiting = waitingCount(data, pendingGates.length);
  const running = data.runningDirections.length + data.runningTasks.length;
  const topicOf = (v: VoyageRead) => byProject(v.project_id)?.name;

  return (
    <Drawer
      open={open}
      onClose={onClose}
      title={<span className="act-title">{tr('任务与待办', 'Activity')}</span>}
    >
      <section className="act-sec" data-activity="waiting">
        <h3 className="group-label">
          {tr('等你处理', 'Waiting for you')}
          {waiting > 0 ? ` · ${waiting}` : ''}
        </h3>
        {gatesError && (
          <ErrorState
            compact
            title={tr('无法加载审批，请确认本机引擎正在运行', 'Couldn’t load approvals. Check that the local engine is running.')}
            onRetry={onRetryGates}
          />
        )}
        {waiting === 0 && !gatesError ? (
          <div className="act-empty">{tr('没有等你处理的事', 'Nothing is waiting for you')}</div>
        ) : (
          <>
            {(data.waitingDirections.length > 0 || data.waitingTasks.length > 0) && (
              <div className="act-rows">
                {data.waitingDirections.map((d) => (
                  <DirectionRow key={d.projectId} d={d} onNavigate={onClose} />
                ))}
                {data.waitingTasks.map((v) => (
                  <TaskRow key={v.id} v={v} topicName={topicOf(v)} onNavigate={onClose} />
                ))}
              </div>
            )}
            {pendingGates.length > 0 && (
              <div className="col gap10" style={{ marginTop: 10 }}>
                {pendingGates.map((g) => (
                  <GateCard
                    key={g.id}
                    gate={g}
                    expanded={expandedGate === g.id}
                    onToggle={() => onToggleGate(g.id)}
                    onDecide={onDecide}
                    deciding={deciding}
                  />
                ))}
              </div>
            )}
          </>
        )}
      </section>

      <section className="act-sec" data-activity="running">
        <h3 className="group-label">
          {tr('正在运行', 'Running')}
          {running > 0 ? ` · ${running}` : ''}
        </h3>
        {voyagesQuery.isLoading ? (
          <LoadingState compact rows={2} />
        ) : running === 0 ? (
          <div className="act-empty">{tr('现在没有任务在运行', 'Nothing is running right now')}</div>
        ) : (
          <div className="act-rows">
            {data.runningDirections.map((d) => (
              <DirectionRow key={d.projectId} d={d} onNavigate={onClose} />
            ))}
            {data.runningTasks.map((v) => (
              <TaskRow key={v.id} v={v} topicName={topicOf(v)} onNavigate={onClose} />
            ))}
          </div>
        )}
      </section>

      <section className="act-sec" data-activity="recent">
        <h3 className="group-label">{tr('最近结束', 'Recently ended')}</h3>
        {voyagesQuery.isError ? (
          <ErrorState
            compact
            title={tr('无法加载任务，请确认本机引擎正在运行', 'Couldn’t load tasks. Check that the local engine is running.')}
            onRetry={() => void voyagesQuery.refetch()}
          />
        ) : data.recentTasks.length === 0 ? (
          <div className="act-empty">{tr('还没有结束的任务', 'No finished tasks yet')}</div>
        ) : (
          <div className="act-rows">
            {data.recentTasks.map((v) => (
              <TaskRow key={v.id} v={v} topicName={topicOf(v)} onNavigate={onClose} />
            ))}
          </div>
        )}
        <div className="act-foot">
          <Link className="btn btn-ghost sm" to="/activity" onClick={onClose}>
            {tr('查看全部任务', 'View all tasks')}
          </Link>
          <button type="button" className="btn btn-plain sm" aria-expanded={showDecided} onClick={() => setShowDecided((s) => !s)}>
            {tr('已处理的审批', 'Past approvals')}
            <Icon name="chevDown" size={11} style={{ transform: showDecided ? 'rotate(180deg)' : undefined }} />
          </button>
        </div>
        {showDecided && (
          <div className="col gap10" style={{ marginTop: 10 }}>
            {decidedQuery.isLoading ? (
              <LoadingState compact />
            ) : decided.length === 0 ? (
              <div className="act-empty">{tr('还没有处理过的审批', 'No past approvals')}</div>
            ) : (
              decided.map((g) => (
                <GateCard key={g.id} gate={g} expanded={expandedGate === g.id} onToggle={() => onToggleGate(g.id)} onDecide={onDecide} />
              ))
            )}
          </div>
        )}
      </section>
    </Drawer>
  );
}
