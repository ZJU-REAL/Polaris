import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { StatusPill } from '../../components/ui/StatusPill';
import { EmptyState } from '../../components/ui/EmptyState';
import { Timeline, TimelineItem } from '../../components/ui/Timeline';
import { toast } from '../../components/ui/Toast';
import { Markdown } from '../../lib/markdown';
import { useShell } from '../../app/AppShell';
import { fmtDuration, fmtTime } from '../../lib/format';
import {
  api,
  ApiError,
  EXPERIMENT_TERMINAL,
  type ExperimentDetail,
  type ExperimentPlan,
  type VoyageStepRead,
} from '../../lib/api';
import { budgetText, HypChip } from './shared';
import { RunTab } from './RunTab';

/* ============================================================
   /experiment/:id — 实验详情：Plan / Setup / Run / Report 四 Tab。
   数据源 GET /experiments/{id}（活动状态 5s 轮询 + WS invalidate），
   Setup 复用关联 voyage 的 steps，Run 内嵌 SSE 日志与指标图。
   ============================================================ */

type TabKey = 'plan' | 'setup' | 'run' | 'report';

const TABS: { k: TabKey; label: string }[] = [
  { k: 'plan', label: 'Plan 计划' },
  { k: 'setup', label: 'Setup 环境' },
  { k: 'run', label: 'Run 运行' },
  { k: 'report', label: 'Report 报告' },
];

/* ---------------- Plan ---------------- */

function planStepText(s: NonNullable<ExperimentPlan['steps']>[number]): string {
  if (typeof s === 'string') return s;
  return s.title ?? s.desc ?? s.description ?? JSON.stringify(s);
}

function PlanTab({ exp, onOpenGates }: { exp: ExperimentDetail; onOpenGates: () => void }) {
  const plan = exp.plan;

  return (
    <div className="fadeup" style={{ maxWidth: 860 }}>
      {exp.status === 'awaiting_gate' && (
        <div
          className="row gap8"
          style={{
            marginBottom: 18,
            padding: '11px 16px',
            background: 'var(--warn-bg)',
            color: 'var(--warn-tx)',
            borderRadius: 10,
            fontSize: 12.5,
            fontWeight: 600,
          }}
        >
          <Icon name="gate" size={15} />
          实验已暂停，等待算力预算审批：消耗真实算力前需人工确认方案与预算。
          <button className="btn btn-primary sm" style={{ marginLeft: 'auto' }} onClick={onOpenGates}>
            前往审批
          </button>
        </div>
      )}

      {!plan ? (
        <div className="card">
          <EmptyState
            compact
            icon="sparkle"
            title={exp.status === 'planning' ? '计划生成中…' : '计划尚未生成'}
            desc="系统读入 idea 内容与相关 wiki 页后自动产出假设清单、复现策略与预算估计。"
          />
        </div>
      ) : (
        <>
          {/* 假设清单 */}
          <span className="section-h" style={{ marginBottom: 12 }}>
            <Icon name="sparkle" size={15} style={{ color: 'var(--accent)' }} />
            假设清单 <span className="en-label" style={{ fontSize: 11 }}>Hypotheses</span>
          </span>
          {(plan.hypotheses ?? []).length === 0 ? (
            <div className="card empty" style={{ padding: 24, marginBottom: 22 }}>计划中未包含假设清单</div>
          ) : (
            <div className="col gap8" style={{ marginBottom: 22 }}>
              {(plan.hypotheses ?? []).map((h, i) => (
                <div key={i} className="card" style={{ padding: '12px 16px' }}>
                  <div className="row gap12">
                    <span className="mono" style={{ fontSize: 11, color: 'var(--text-4)', flexShrink: 0 }}>H{i + 1}</span>
                    <span style={{ fontSize: 13, flex: 1, lineHeight: 1.5 }}>{h.text}</span>
                    <HypChip status={h.status} />
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* 复现策略 */}
          <span className="section-h" style={{ marginBottom: 12 }}>
            <Icon name="layers" size={15} style={{ color: 'var(--accent)' }} />
            复现策略 <span className="en-label" style={{ fontSize: 11 }}>Repro strategy</span>
          </span>
          <div className="card card-pad" style={{ marginBottom: 22, background: 'var(--surface-2)' }}>
            <p style={{ fontSize: 13, lineHeight: 1.65, margin: 0, color: 'var(--text-2)', whiteSpace: 'pre-wrap' }}>
              {plan.repro_strategy || '（计划中未包含复现策略）'}
            </p>
          </div>

          {/* 实验步骤 */}
          <span className="section-h" style={{ marginBottom: 12 }}>
            <Icon name="compass" size={15} style={{ color: 'var(--accent)' }} />
            实验步骤 <span className="en-label" style={{ fontSize: 11 }}>Steps</span>
          </span>
          {(plan.steps ?? []).length === 0 ? (
            <div className="card empty" style={{ padding: 24, marginBottom: 22 }}>计划中未包含步骤列表</div>
          ) : (
            <div className="card" style={{ overflow: 'hidden', marginBottom: 22 }}>
              {(plan.steps ?? []).map((s, i, arr) => (
                <div
                  key={i}
                  className="row gap12"
                  style={{ padding: '11px 18px', borderBottom: i < arr.length - 1 ? '0.5px solid var(--border)' : 'none', alignItems: 'flex-start' }}
                >
                  <span className="mono" style={{ fontSize: 11, color: 'var(--accent-text)', flexShrink: 0, marginTop: 2 }}>
                    {i + 1}
                  </span>
                  <span style={{ fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.5 }}>{planStepText(s)}</span>
                </div>
              ))}
            </div>
          )}

          {/* 预算 */}
          <span className="section-h" style={{ marginBottom: 12 }}>
            <Icon name="clock" size={15} style={{ color: 'var(--accent)' }} />
            预算 <span className="en-label" style={{ fontSize: 11 }}>Budget</span>
          </span>
          <div className="row gap12" style={{ alignItems: 'stretch' }}>
            <div className="card card-pad" style={{ flex: 1 }}>
              <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 6 }}>硬上限（超限自动 kill）</div>
              <div className="mono" style={{ fontSize: 18, fontWeight: 700 }}>{budgetText(exp.budget)}</div>
            </div>
            <div className="card card-pad" style={{ flex: 1.6, background: 'var(--surface-2)' }}>
              <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 6 }}>计划估计 budget_estimate</div>
              <div style={{ fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.5, whiteSpace: 'pre-wrap' }}>
                {plan.budget_estimate
                  ? typeof plan.budget_estimate === 'string'
                    ? plan.budget_estimate
                    : JSON.stringify(plan.budget_estimate, null, 2)
                  : '—'}
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

/* ---------------- Setup ---------------- */

interface GeneratedFile {
  name: string;
  content: string;
}

/** 从 voyage step 的 observation.files 里提取生成的代码文件（同名取最新）。 */
function extractFiles(steps: VoyageStepRead[]): GeneratedFile[] {
  const byName = new Map<string, string>();
  for (const s of steps) {
    const obs = s.observation;
    if (!obs || typeof obs !== 'object') continue;
    const files = (obs as { files?: unknown }).files;
    if (!Array.isArray(files)) continue;
    for (const f of files) {
      if (!f || typeof f !== 'object') continue;
      const rec = f as Record<string, unknown>;
      const name =
        typeof rec.name === 'string' ? rec.name : typeof rec.path === 'string' ? rec.path : null;
      if (!name) continue;
      byName.set(name, typeof rec.content === 'string' ? rec.content : '');
    }
  }
  return [...byName.entries()].map(([name, content]) => ({ name, content }));
}

const SETUP_STEP_RE = /setup|env|smoke|ssh|code|file|install|provision|venv|环境|冒烟|代码|连接/i;

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

function FileCard({ file }: { file: GeneratedFile }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="card" style={{ overflow: 'hidden' }}>
      <button
        className="row gap8"
        onClick={() => setOpen(!open)}
        style={{
          width: '100%',
          border: 'none',
          background: 'transparent',
          cursor: 'pointer',
          padding: '11px 16px',
          textAlign: 'left',
        }}
      >
        <Icon name="file" size={14} style={{ color: 'var(--accent)', flexShrink: 0 }} />
        <span className="mono" style={{ fontSize: 12, fontWeight: 600, flex: 1 }}>{file.name}</span>
        <span className="mono muted" style={{ fontSize: 10.5 }}>{file.content.length.toLocaleString()} chars</span>
        <Icon name="chevDown" size={13} style={{ transform: open ? 'rotate(180deg)' : 'none', transition: 'transform .15s', color: 'var(--text-3)' }} />
      </button>
      {open && (
        <pre
          className="codeblock scroll"
          style={{ margin: 0, borderRadius: 0, borderTop: '0.5px solid var(--border)', fontSize: 11, maxHeight: 360, overflow: 'auto' }}
        >
          {file.content || '（空文件）'}
        </pre>
      )}
    </div>
  );
}

function SetupTab({ exp }: { exp: ExperimentDetail }) {
  const navigate = useNavigate();
  const vid = exp.voyage_id;
  const { data: voyage, isLoading, isError } = useQuery({
    queryKey: ['voyage', vid],
    queryFn: () => api.getVoyage(vid!),
    enabled: !!vid,
    retry: false,
  });

  if (!vid) {
    return (
      <div className="card">
        <EmptyState compact icon="server" title="尚未关联任务" desc="实验创建后会入队一个 kind=experiment 的 voyage。" />
      </div>
    );
  }
  if (isLoading) return <div className="empty" style={{ padding: 40 }}>加载环境搭建步骤…</div>;
  if (isError || !voyage) {
    return (
      <div className="card">
        <EmptyState compact icon="x" title="无法加载关联任务" desc="后端不可用或接口尚未就绪。" />
      </div>
    );
  }

  const all = [...(voyage.steps ?? [])].sort((a, b) => a.seq - b.seq);
  const matched = all.filter((s) => SETUP_STEP_RE.test(`${s.action} ${s.title}`));
  const steps = matched.length > 0 ? matched : all;
  const files = extractFiles(all);

  return (
    <div className="fadeup" style={{ maxWidth: 860 }}>
      <div className="row gap8" style={{ marginBottom: 12, justifyContent: 'space-between' }}>
        <span className="section-h">
          <Icon name="server" size={15} style={{ color: 'var(--accent)' }} />
          环境搭建步骤 <span className="en-label" style={{ fontSize: 11 }}>setup · smoke（来自关联任务）</span>
        </span>
        <button
          className="btn btn-ghost sm mono"
          style={{ fontSize: 11 }}
          onClick={() => navigate(`/voyages/${vid}`)}
        >
          voyage {vid.slice(0, 8)} →
        </button>
      </div>
      {steps.length === 0 ? (
        <div className="card empty" style={{ padding: 32, marginBottom: 24 }}>任务尚未产生步骤</div>
      ) : (
        <div style={{ marginBottom: 24 }}>
          <Timeline>
            {steps.map((s, i) => {
              const m = stepMarker(s);
              return (
                <TimelineItem key={s.id} marker={s.seq} markerBg={m.bg} markerColor={m.color} last={i === steps.length - 1}>
                  <div className="card" style={{ padding: '12px 16px' }}>
                    <div className="row gap8" style={{ flexWrap: 'wrap' }}>
                      <span style={{ fontSize: 13, fontWeight: 650 }}>{s.title}</span>
                      <span className="tag mono" style={{ fontSize: 10.5 }}>{s.action}</span>
                      <div style={{ marginLeft: 'auto' }}>
                        <StatusPill status={s.status} sm />
                      </div>
                    </div>
                    {s.started_at && (
                      <div className="mono muted" style={{ fontSize: 11, marginTop: 7 }}>
                        {fmtTime(s.started_at)} · 耗时 {s.finished_at ? fmtDuration(s.started_at, s.finished_at) : '进行中'}
                      </div>
                    )}
                    {s.verdict && !s.verdict.passed && s.verdict.reason && (
                      <div style={{ marginTop: 7, fontSize: 12, color: 'var(--danger-tx)', lineHeight: 1.5 }}>
                        自动校验：{s.verdict.reason}
                      </div>
                    )}
                  </div>
                </TimelineItem>
              );
            })}
          </Timeline>
        </div>
      )}

      <span className="section-h" style={{ marginBottom: 12 }}>
        <Icon name="file" size={15} style={{ color: 'var(--accent)' }} />
        生成的代码文件 <span className="en-label" style={{ fontSize: 11 }}>Generated files · {files.length}</span>
      </span>
      {files.length === 0 ? (
        <div className="card empty" style={{ padding: 28 }}>
          暂无生成文件（建环境阶段 LLM 生成 train.py / eval.py / requirements.txt / run.sh 后显示在这里）
        </div>
      ) : (
        <div className="col gap8">
          {files.map((f) => (
            <FileCard key={f.name} file={f} />
          ))}
        </div>
      )}
    </div>
  );
}

/* ---------------- Report ---------------- */

function ReportTab({ exp }: { exp: ExperimentDetail }) {
  if (!exp.report) {
    return (
      <div className="card">
        <EmptyState
          compact
          icon="pen"
          title="报告尚未生成"
          desc={
            EXPERIMENT_TERMINAL.has(exp.status)
              ? '该实验未产出报告。'
              : '正式运行结束后，agent 会汇总指标与日志尾部生成 markdown 报告。'
          }
        />
      </div>
    );
  }
  return (
    <div className="fadeup card card-pad" style={{ maxWidth: 860 }}>
      <Markdown source={exp.report} />
    </div>
  );
}

/* ---------------- 页面 ---------------- */

export function ExperimentDetailPage() {
  const { id = '' } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { openGates } = useShell();
  const [tab, setTab] = useState<TabKey>('plan');
  const defaultedRef = useRef(false);

  const { data: exp, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['experiment', id],
    queryFn: () => api.getExperiment(id),
    enabled: !!id,
    retry: false,
    refetchInterval: (q) =>
      q.state.data && !EXPERIMENT_TERMINAL.has(q.state.data.status) ? 5_000 : false,
  });

  // 首次加载后按状态定位默认 Tab
  useEffect(() => {
    if (!exp || defaultedRef.current) return;
    defaultedRef.current = true;
    if (exp.status === 'setup') setTab('setup');
    else if (exp.status === 'running') setTab('run');
    else if (exp.status === 'reporting' || (exp.status === 'done' && exp.report)) setTab('report');
    else if (exp.status === 'done' || exp.status === 'failed') setTab('run');
  }, [exp]);

  const cancelMutation = useMutation({
    mutationFn: () => api.cancelExperiment(id),
    onSuccess: () => {
      toast('实验已取消 · experiment cancelled', 'ok');
      void queryClient.invalidateQueries({ queryKey: ['experiment', id] });
      void queryClient.invalidateQueries({ queryKey: ['experiments'] });
      void queryClient.invalidateQueries({ queryKey: ['voyages'] });
    },
    onError: (e) => toast(`取消失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  if (isLoading) {
    return (
      <div className="page fadeup">
        <div className="empty" style={{ padding: 80 }}>加载实验详情…</div>
      </div>
    );
  }
  if (isError || !exp) {
    const notFound = error instanceof ApiError && error.status === 404;
    return (
      <div className="page fadeup">
        <div className="card card-pad" style={{ textAlign: 'center', padding: 60 }}>
          <div style={{ fontSize: 15, fontWeight: 650, marginBottom: 8 }}>
            {notFound ? '实验不存在' : '无法加载实验详情'}
          </div>
          <div style={{ fontSize: 12.5, color: 'var(--text-3)', marginBottom: 18 }}>
            {error instanceof Error ? error.message : '后端不可用，请稍后重试'}
          </div>
          <div className="row gap8" style={{ justifyContent: 'center' }}>
            <button className="btn btn-soft" onClick={() => void refetch()}>重试 retry</button>
            <button className="btn btn-ghost" onClick={() => navigate('/experiment')}>返回列表</button>
          </div>
        </div>
      </div>
    );
  }

  const active = !EXPERIMENT_TERMINAL.has(exp.status);

  return (
    <div className="page fadeup" style={{ maxWidth: 1080 }}>
      {/* 页头 */}
      <div className="row" style={{ alignItems: 'flex-start', marginBottom: 4 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="h-eyebrow row gap8">
            <span className="row gap6" style={{ cursor: 'pointer' }} onClick={() => navigate('/experiment')}>
              ← Experiment Lab
            </span>
            <span className="mono" style={{ textTransform: 'none', color: 'var(--text-4)' }}>{exp.id.slice(0, 8)}</span>
          </div>
          <h1 className="h-title" style={{ fontSize: 20 }}>{exp.idea_title}</h1>
          <div className="row gap8" style={{ marginTop: 10, flexWrap: 'wrap' }}>
            <StatusPill status={exp.status} sm />
            <span className="pill sm">
              <Icon name="server" size={11} />
              {exp.server_host ?? '未分配'}
            </span>
            <span className="pill sm mono" style={{ background: 'var(--surface-3)' }}>{budgetText(exp.budget)}</span>
            {exp.workdir && (
              <span className="mono muted" style={{ fontSize: 11 }}>{exp.workdir}</span>
            )}
            <span className="mono muted" style={{ fontSize: 11 }}>
              创建 {fmtTime(exp.created_at)} · 耗时 {fmtDuration(exp.created_at, active ? null : exp.updated_at)}
            </span>
          </div>
        </div>
        <div className="row gap8" style={{ flexShrink: 0 }}>
          <button
            className="btn btn-ghost sm"
            style={{ visibility: exp.idea_id ? 'visible' : 'hidden' }}
            onClick={() => navigate(`/ideas/${exp.idea_id}`)}
          >
            <Icon name="bulb" size={13} />
            查看 idea
          </button>
          {active && (
            <button
              className="btn btn-ghost"
              disabled={cancelMutation.isPending}
              onClick={() => {
                if (window.confirm('确定取消该实验？将取消关联任务并尝试终止远端进程。')) {
                  cancelMutation.mutate();
                }
              }}
            >
              <Icon name="x" size={13} />
              {cancelMutation.isPending ? '取消中…' : '取消实验'}
            </button>
          )}
        </div>
      </div>

      {/* Tab bar */}
      <div className="row" style={{ gap: 2, borderBottom: '0.5px solid var(--border)', marginBottom: 22 }}>
        {TABS.map((t) => (
          <button
            key={t.k}
            onClick={() => setTab(t.k)}
            style={{
              border: 'none',
              background: 'none',
              cursor: 'pointer',
              padding: '10px 16px',
              fontSize: 13,
              fontWeight: 600,
              fontFamily: 'var(--sans)',
              color: tab === t.k ? 'var(--text)' : 'var(--text-3)',
              borderBottom: tab === t.k ? '2px solid var(--accent)' : '2px solid transparent',
              marginBottom: -1,
            }}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'plan' && <PlanTab exp={exp} onOpenGates={() => openGates(null)} />}
      {tab === 'setup' && <SetupTab exp={exp} />}
      {tab === 'run' && <RunTab exp={exp} active={active} />}
      {tab === 'report' && <ReportTab exp={exp} />}
    </div>
  );
}
