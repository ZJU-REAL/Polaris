import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { StatusPill } from '../../components/ui/StatusPill';
import { Timeline, TimelineItem } from '../../components/ui/Timeline';
import { toast } from '../../components/ui/Toast';
import { useShell } from '../../app/AppShell';
import { subscribeSse } from '../../lib/sse';
import { fmtDuration, fmtTime, fmtTokens } from '../../lib/format';
import {
  api,
  ApiError,
  VOYAGE_TERMINAL,
  type VoyageDetail,
  type VoyageStatus,
  type VoyageStepRead,
} from '../../lib/api';

/* ============================================================
   /voyages/:id — 航程详情：状态机进度条 + 步骤时间线 + SSE 实时。
   活动状态订阅 /voyages/{id}/events，事件与 TanStack Query 缓存合并。
   ============================================================ */

// —— 状态机进度条 ——

const MACHINE = [
  { key: 'planning', zh: '规划', en: 'planning' },
  { key: 'executing', zh: '执行', en: 'executing' },
  { key: 'verifying', zh: '自检', en: 'verifying' },
  { key: 'done', zh: '完成', en: 'done' },
] as const;

function machineIndex(status: VoyageStatus): number {
  switch (status) {
    case 'planning':
      return 0;
    case 'executing':
    case 'replanning':
    case 'paused_gate':
    case 'paused_error':
      return 1;
    case 'verifying':
      return 2;
    case 'done':
    case 'failed':
    case 'cancelled':
      return 3;
  }
}

function MachineBar({ status, onOpenGates }: { status: VoyageStatus; onOpenGates: () => void }) {
  const idx = machineIndex(status);
  const paused = status === 'paused_gate';
  const errored = status === 'paused_error' || status === 'failed' || status === 'cancelled';
  return (
    <div>
      <div className="sm-bar">
        {MACHINE.map((m, i) => {
          const isCur = i === idx;
          const isDone = i < idx || (i === idx && status === 'done');
          let bg = 'var(--surface-3)';
          let color = 'var(--text-3)';
          if (isDone) {
            bg = 'var(--ok-bg)';
            color = 'var(--ok-tx)';
          }
          if (isCur && status !== 'done') {
            if (paused) {
              bg = 'var(--warn-bg)';
              color = 'var(--warn-tx)';
            } else if (errored) {
              bg = 'var(--danger-bg)';
              color = 'var(--danger-tx)';
            } else {
              bg = 'var(--accent)';
              color = '#fff';
            }
          }
          return (
            <div key={m.key} className="row" style={{ flex: i < MACHINE.length - 1 ? 1 : 'none' }}>
              <span
                className={'sm-node' + (isCur && !paused && !errored && status !== 'done' ? ' pulse' : '')}
                style={{ background: bg, color }}
              >
                {isDone ? <Icon name="check" size={11} /> : null}
                {m.zh}
                <span style={{ opacity: 0.7, fontSize: '0.88em' }}>{m.en}</span>
              </span>
              {i < MACHINE.length - 1 && (
                <span className="sm-link" style={{ background: i < idx ? 'var(--ok)' : 'var(--border-2)' }} />
              )}
            </div>
          );
        })}
      </div>
      {paused && (
        <div
          className="row gap8"
          style={{
            marginTop: 12,
            padding: '10px 14px',
            background: 'var(--warn-bg)',
            color: 'var(--warn-tx)',
            borderRadius: 10,
            fontSize: 12.5,
            fontWeight: 600,
          }}
        >
          <Icon name="gate" size={15} />
          航程在闸门处暂停，等待人工审批后继续。
          <button className="btn btn-primary sm" style={{ marginLeft: 'auto' }} onClick={onOpenGates}>
            前往审批
          </button>
        </div>
      )}
      {status === 'paused_error' && (
        <div className="row gap8" style={{ marginTop: 12, padding: '10px 14px', background: 'var(--danger-bg)', color: 'var(--danger-tx)', borderRadius: 10, fontSize: 12.5 }}>
          <Icon name="x" size={14} />
          航程因错误暂停，等待重试或取消。
        </div>
      )}
    </div>
  );
}

// —— 步骤卡 ——

function stepMarker(step: VoyageStepRead): { bg: string; color: string } {
  if (step.verdict && !step.verdict.passed) return { bg: 'var(--danger-bg)', color: 'var(--danger-tx)' };
  switch (step.status) {
    case 'done':
      return { bg: 'var(--ok-bg)', color: 'var(--ok-tx)' };
    case 'running':
      return { bg: 'var(--accent)', color: '#fff' };
    case 'failed':
      return { bg: 'var(--danger-bg)', color: 'var(--danger-tx)' };
    default:
      return { bg: 'var(--surface-2)', color: 'var(--text-3)' };
  }
}

function ObservationBlock({ observation }: { observation: unknown }) {
  const [open, setOpen] = useState(false);
  if (observation === null || observation === undefined) return null;
  const text = typeof observation === 'string' ? observation : JSON.stringify(observation, null, 2);
  const preview = text.length > 160 ? `${text.slice(0, 160)}…` : text;
  return (
    <div style={{ marginTop: 10 }}>
      <button
        className="row gap6"
        onClick={() => setOpen(!open)}
        style={{ border: 'none', background: 'transparent', cursor: 'pointer', padding: 0, fontSize: 11.5, fontWeight: 600, color: 'var(--text-3)' }}
      >
        <Icon name="chevDown" size={12} style={{ transform: open ? 'rotate(180deg)' : 'none', transition: 'transform .15s' }} />
        观察 observation
      </button>
      <div className="codeblock scroll" style={{ fontSize: 11, marginTop: 6, maxHeight: open ? 400 : 'none', overflowY: 'auto', whiteSpace: 'pre-wrap' }}>
        {open ? text : preview}
      </div>
    </div>
  );
}

function StepCard({ step }: { step: VoyageStepRead }) {
  return (
    <div className="card" style={{ padding: '14px 16px' }}>
      <div className="row gap8" style={{ flexWrap: 'wrap' }}>
        <span style={{ fontSize: 13.5, fontWeight: 650 }}>{step.title}</span>
        <span className="tag mono" style={{ fontSize: 10.5 }}>{step.action}</span>
        <div style={{ marginLeft: 'auto' }}>
          <StatusPill status={step.status} sm />
        </div>
      </div>
      <div className="row gap10" style={{ marginTop: 8, flexWrap: 'wrap' }}>
        {step.verdict && (
          <span
            className="pill sm"
            style={
              step.verdict.passed
                ? { background: 'var(--ok-bg)', color: 'var(--ok-tx)' }
                : { background: 'var(--danger-bg)', color: 'var(--danger-tx)' }
            }
            title={step.verdict.reason}
          >
            <Icon name={step.verdict.passed ? 'check' : 'x'} size={11} />
            Sextant {step.verdict.passed ? 'passed' : 'failed'}
          </span>
        )}
        {step.tokens !== null && (
          <span className="mono muted" style={{ fontSize: 11 }}>
            <Icon name="cpu" size={11} style={{ display: 'inline-block', verticalAlign: '-1.5px', marginRight: 4 }} />
            {fmtTokens(step.tokens)} tok
          </span>
        )}
        {step.started_at && (
          <span className="mono muted" style={{ fontSize: 11 }}>
            {fmtTime(step.started_at)} · 耗时 {step.finished_at ? fmtDuration(step.started_at, step.finished_at) : '进行中'}
          </span>
        )}
      </div>
      {step.verdict && !step.verdict.passed && step.verdict.reason && (
        <div style={{ marginTop: 8, fontSize: 12, color: 'var(--danger-tx)', lineHeight: 1.5 }}>
          {step.verdict.reason}
        </div>
      )}
      <ObservationBlock observation={step.observation} />
    </div>
  );
}

// —— 页面 ——

export function VoyageDetailPage() {
  const { id = '' } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { openGates } = useShell();
  const [logs, setLogs] = useState<string[]>([]);
  const [live, setLive] = useState(false);

  const { data: voyage, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['voyage', id],
    queryFn: () => api.getVoyage(id),
    retry: false,
    enabled: !!id,
  });

  const cancelMutation = useMutation({
    mutationFn: () => api.cancelVoyage(id),
    onSuccess: () => {
      toast('航程已取消', 'ok');
      void queryClient.invalidateQueries({ queryKey: ['voyage', id] });
      void queryClient.invalidateQueries({ queryKey: ['voyages'] });
    },
    onError: (err) => toast(`取消失败：${err instanceof Error ? err.message : String(err)}`, 'error'),
  });

  const active = !!voyage && !VOYAGE_TERMINAL.has(voyage.status);

  // —— SSE 实时订阅（活动状态时） ——
  useEffect(() => {
    if (!id || !active) return;
    const stop = subscribeSse(`/voyages/${id}/events`, {
      onOpen: () => setLive(true),
      onError: () => setLive(false),
      onEvent: (event, dataStr) => {
        let payload: unknown;
        try {
          payload = JSON.parse(dataStr);
        } catch {
          return;
        }
        if (event === 'status') {
          const p = payload as { status: VoyageStatus; cursor: number | null };
          queryClient.setQueryData<VoyageDetail>(['voyage', id], (old) =>
            old ? { ...old, status: p.status, cursor: p.cursor ?? old.cursor } : old,
          );
          if (VOYAGE_TERMINAL.has(p.status)) {
            void queryClient.invalidateQueries({ queryKey: ['voyages'] });
            void queryClient.invalidateQueries({ queryKey: ['voyage', id] });
          }
        } else if (event === 'step') {
          const p = payload as { step: VoyageStepRead };
          if (!p.step) return;
          queryClient.setQueryData<VoyageDetail>(['voyage', id], (old) => {
            if (!old) return old;
            const steps = old.steps ?? [];
            const i = steps.findIndex((s) => s.id === p.step.id);
            const next = i >= 0 ? steps.map((s, j) => (j === i ? p.step : s)) : [...steps, p.step];
            next.sort((a, b) => a.seq - b.seq);
            return { ...old, steps: next };
          });
        } else if (event === 'log') {
          const p = payload as { message?: string };
          if (p.message) setLogs((l) => [...l.slice(-199), p.message as string]);
        }
      },
    });
    return () => {
      stop();
      setLive(false);
    };
  }, [id, active, queryClient]);

  if (isLoading) {
    return (
      <div className="page fadeup">
        <div className="empty" style={{ padding: 80 }}>加载中…</div>
      </div>
    );
  }
  if (isError || !voyage) {
    const notFound = error instanceof ApiError && error.status === 404;
    return (
      <div className="page fadeup">
        <div className="card card-pad" style={{ textAlign: 'center', padding: 60 }}>
          <div style={{ fontSize: 15, fontWeight: 650, marginBottom: 8 }}>
            {notFound ? '航程不存在' : '无法加载航程详情'}
          </div>
          <div style={{ fontSize: 12.5, color: 'var(--text-3)', marginBottom: 18 }}>
            {error instanceof Error ? error.message : '后端不可用，请稍后重试'}
          </div>
          <div className="row gap8" style={{ justifyContent: 'center' }}>
            <button className="btn btn-soft" onClick={() => void refetch()}>重试 retry</button>
            <button className="btn btn-ghost" onClick={() => navigate('/voyages')}>返回列表</button>
          </div>
        </div>
      </div>
    );
  }

  const steps = [...(voyage.steps ?? [])].sort((a, b) => a.seq - b.seq);
  const totalTokens = steps.reduce((acc, s) => acc + (s.tokens ?? 0), 0);

  return (
    <div className="page fadeup" style={{ maxWidth: 920 }}>
      {/* 页头 */}
      <div className="row" style={{ alignItems: 'flex-start', marginBottom: 20 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="h-eyebrow row gap8">
            <span
              className="row gap6"
              style={{ cursor: 'pointer' }}
              onClick={() => navigate('/voyages')}
            >
              ← Voyages
            </span>
            <span className="mono" style={{ textTransform: 'none', color: 'var(--text-4)' }}>{voyage.id.slice(0, 8)}</span>
            {live && (
              <span className="pill sm" style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>
                <span className="dot pulse" />
                LIVE
              </span>
            )}
          </div>
          <h1 className="h-title" style={{ fontSize: 21 }}>{voyage.goal}</h1>
          <div className="row gap8" style={{ marginTop: 10, flexWrap: 'wrap' }}>
            <span className="pill sm mono" style={{ background: 'var(--surface-3)' }}>{voyage.kind}</span>
            <StatusPill status={voyage.status} sm />
            <span className="mono muted" style={{ fontSize: 11 }}>
              创建 {fmtTime(voyage.created_at)} · 耗时 {fmtDuration(voyage.created_at, active ? null : voyage.updated_at)}
            </span>
            {totalTokens > 0 && (
              <span className="mono muted" style={{ fontSize: 11 }}>· {fmtTokens(totalTokens)} tokens</span>
            )}
          </div>
        </div>
        {active && (
          <button className="btn btn-ghost" disabled={cancelMutation.isPending} onClick={() => cancelMutation.mutate()}>
            <Icon name="x" size={13} />
            取消航程
          </button>
        )}
      </div>

      {/* 状态机进度条 */}
      <div className="card card-pad" style={{ marginBottom: 20 }}>
        <MachineBar status={voyage.status} onOpenGates={() => openGates(null)} />
      </div>

      {/* 步骤时间线 */}
      <div className="row" style={{ marginBottom: 12 }}>
        <span className="section-h">
          <Icon name="compass" size={15} style={{ color: 'var(--accent)' }} />
          步骤时间线 <span className="en-label" style={{ fontSize: 11 }}>Steps</span>
        </span>
      </div>
      {steps.length === 0 ? (
        <div className="card card-pad empty" style={{ padding: 40 }}>
          Navigator 正在规划步骤…
        </div>
      ) : (
        <Timeline>
          {steps.map((s, i) => {
            const m = stepMarker(s);
            return (
              <TimelineItem key={s.id} marker={s.seq} markerBg={m.bg} markerColor={m.color} last={i === steps.length - 1}>
                <StepCard step={s} />
              </TimelineItem>
            );
          })}
        </Timeline>
      )}

      {/* 实时日志 */}
      {logs.length > 0 && (
        <>
          <div className="row" style={{ margin: '20px 0 12px' }}>
            <span className="section-h">
              <Icon name="file" size={15} style={{ color: 'var(--accent)' }} />
              实时日志 <span className="en-label" style={{ fontSize: 11 }}>Live log</span>
            </span>
          </div>
          <div className="codeblock scroll" style={{ fontSize: 11, maxHeight: 240, overflowY: 'auto', whiteSpace: 'pre-wrap' }}>
            {logs.map((l, i) => (
              <div key={i}>{l}</div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
