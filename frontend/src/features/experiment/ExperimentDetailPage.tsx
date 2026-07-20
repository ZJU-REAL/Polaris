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
  type EvalProtocol,
  type ExperimentCondition,
  type ExperimentContainer,
  type ExperimentDataset,
  type ExperimentDetail,
  type ExperimentPlan,
  type VoyageStepRead,
} from '../../lib/api';
import { tr } from '../../lib/i18n';
import { budgetText, HypChip } from './shared';
import { RunTab } from './RunTab';
import { CodeTab } from './CodeTab';
import { SysinfoPanel } from '../../components/ui/SysinfoPanel';
import { ExperimentFigures } from './ExperimentFigures';

/* ============================================================
   /experiment/:id — 实验详情：Plan / Setup / Run / Report 四 Tab。
   数据源 GET /experiments/{id}（活动状态 5s 轮询 + WS invalidate），
   Setup 复用关联 voyage 的 steps，Run 内嵌 SSE 日志与指标图。
   ============================================================ */

type TabKey = 'plan' | 'setup' | 'run' | 'code' | 'report';

/* 文案在渲染处 tr()，避免模块级求值不随语言切换 */
const TABS: { k: TabKey; zh: string; en: string }[] = [
  { k: 'plan', zh: '计划', en: 'Plan' },
  { k: 'setup', zh: '环境', en: 'Setup' },
  { k: 'run', zh: '运行与迭代', en: 'Run & iterate' },
  { k: 'code', zh: '代码', en: 'Code' },
  { k: 'report', zh: '报告', en: 'Report' },
];

/* ---------------- Plan ---------------- */

function planStepText(s: NonNullable<ExperimentPlan['steps']>[number]): string {
  if (typeof s === 'string') return s;
  return s.title ?? s.desc ?? s.description ?? JSON.stringify(s);
}

/* 实验类型 / 对照角色 → 大白话（模块级常量只存 zh/en，渲染处再 tr） */
const EXP_KIND_INFO: Record<string, { zh: string; en: string }> = {
  eval: { zh: '评测', en: 'Evaluation' },
  training: { zh: '训练', en: 'Training' },
  agent: { zh: 'Agent', en: 'Agent' },
  analysis: { zh: '分析', en: 'Analysis' },
  other: { zh: '其他', en: 'Other' },
};

const CONDITION_ROLE: Record<string, { zh: string; en: string }> = {
  baseline: { zh: '对照组', en: 'Baseline' },
  treatment: { zh: '处理组', en: 'Treatment' },
};

/** 归一化 container：有镜像名 → 容器运行，返回镜像/GPU/共享内存；无 → null（本机运行）。 */
function containerInfo(
  container: ExperimentContainer | null | undefined,
): { image: string; gpus: string; shm: string } | null {
  if (!container || typeof container !== 'object') return null;
  const image = typeof container.image === 'string' ? container.image.trim() : '';
  if (!image) return null;
  const rawGpus = container.gpus;
  const gpus =
    typeof rawGpus === 'string'
      ? rawGpus.trim()
      : typeof rawGpus === 'number' && Number.isFinite(rawGpus)
        ? String(rawGpus)
        : '';
  const shm = typeof container.shm_size === 'string' ? container.shm_size.trim() : '';
  return { image, gpus, shm };
}

/** 实验类型徽标（plan 缺省时显示「未分类」）。 */
function KindBadge({ kind }: { kind: string | undefined }) {
  const m = kind ? EXP_KIND_INFO[kind] : undefined;
  return (
    <span
      className="pill sm"
      style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}
      title={tr('实验类型', 'Experiment type')}
    >
      <Icon name="flask" size={11} />
      {m ? tr(m.zh, m.en) : kind || tr('未分类', 'Uncategorized')}
    </span>
  );
}

/** 运行环境徽标（页头用，紧凑）：容器运行 / 本机环境。 */
function EnvBadge({ container }: { container: ExperimentContainer | null | undefined }) {
  const info = containerInfo(container);
  return (
    <span
      className="pill sm"
      style={{ background: 'var(--surface-3)', color: 'var(--text-2)' }}
      title={
        info
          ? tr(`在预置容器镜像里运行：${info.image}`, `Runs inside a preset container image: ${info.image}`)
          : tr('直接在本机环境运行（裸机）', 'Runs directly on the host environment (bare metal)')
      }
    >
      <Icon name="cpu" size={11} />
      {info ? tr('容器运行', 'Container') : tr('本机环境', 'Host env')}
    </span>
  );
}

/** 运行环境明细（Plan 页用）：容器镜像 + GPU + 共享内存，或本机环境。 */
function RunEnvironmentRow({ container }: { container: ExperimentContainer | null | undefined }) {
  const info = containerInfo(container);
  return (
    <div className="row gap8" style={{ flexWrap: 'wrap', alignItems: 'center', minWidth: 0 }}>
      <span style={{ fontSize: 11.5, color: 'var(--text-3)', flexShrink: 0 }}>{tr('运行环境', 'Environment')}</span>
      {info ? (
        <>
          <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-2)', flexShrink: 0 }}>
            <Icon name="cpu" size={11} />
            {tr('预置容器', 'Container')}
          </span>
          <span className="tag mono" style={{ fontSize: 11, minWidth: 0, wordBreak: 'break-all' }}>{info.image}</span>
          {info.gpus && (
            <span className="pill sm mono" style={{ background: 'var(--surface-2)', color: 'var(--text-2)', flexShrink: 0 }}>
              GPU {info.gpus}
            </span>
          )}
          {info.shm && (
            <span className="mono muted" style={{ fontSize: 10.5, flexShrink: 0 }}>shm {info.shm}</span>
          )}
        </>
      ) : (
        <span className="pill sm" style={{ background: 'var(--surface-2)', color: 'var(--text-2)', flexShrink: 0 }}>
          <Icon name="server" size={11} />
          {tr('本机环境（裸机运行）', 'Host environment (bare metal)')}
        </span>
      )}
    </div>
  );
}

/** 主指标方向 → 大白话。 */
function metricDirectionText(direction: string | undefined): string {
  if (direction === 'minimize') return tr('越低越好', 'lower is better');
  if (direction === 'maximize') return tr('越高越好', 'higher is better');
  return '';
}

/** 计划概览：实验类型 + 主指标 + 运行环境（缺字段防御式跳过）。 */
function PlanOverview({ exp }: { exp: ExperimentDetail }) {
  const plan = exp.plan!;
  const pm = plan.primary_metric;
  return (
    <div className="card card-pad" style={{ marginBottom: 22 }}>
      <div className="col gap10">
        <div className="row gap8" style={{ flexWrap: 'wrap', alignItems: 'center' }}>
          <span style={{ fontSize: 11.5, color: 'var(--text-3)', flexShrink: 0 }}>{tr('实验类型', 'Type')}</span>
          <KindBadge kind={plan.kind} />
          {pm?.name && (
            <>
              <span style={{ fontSize: 11.5, color: 'var(--text-3)', flexShrink: 0, marginLeft: 6 }}>{tr('主指标', 'Primary metric')}</span>
              <span className="pill sm mono" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
                <Icon name="chart" size={11} />
                {pm.name}
                {metricDirectionText(pm.direction) && (
                  <span style={{ opacity: 0.8 }}> · {metricDirectionText(pm.direction)}</span>
                )}
              </span>
            </>
          )}
        </div>
        <RunEnvironmentRow container={plan.container} />
      </div>
    </div>
  );
}

/** 对照设置：baseline 对照组 vs 各 treatment 处理组。 */
function ConditionsBlock({ conditions }: { conditions: ExperimentCondition[] }) {
  return (
    <div className="col gap8" style={{ marginBottom: 22 }}>
      {conditions.map((c, i) => {
        const roleKey = c.role === 'baseline' ? 'baseline' : 'treatment';
        const roleInfo = CONDITION_ROLE[roleKey]!;
        const isBaseline = roleKey === 'baseline';
        return (
          <div key={`${c.name}-${i}`} className="card" style={{ padding: '12px 16px' }}>
            <div className="row gap8" style={{ flexWrap: 'wrap', alignItems: 'center' }}>
              <span
                className="pill sm"
                style={
                  isBaseline
                    ? { background: 'var(--surface-3)', color: 'var(--text-2)', flexShrink: 0 }
                    : { background: 'var(--accent-soft)', color: 'var(--accent-text)', flexShrink: 0 }
                }
              >
                {tr(roleInfo.zh, roleInfo.en)}
              </span>
              <span style={{ fontSize: 13, fontWeight: 650, minWidth: 0 }}>{c.name}</span>
            </div>
            {c.description && c.description.trim() !== '' && (
              <div style={{ marginTop: 7, fontSize: 12, color: 'var(--text-2)', lineHeight: 1.6, whiteSpace: 'pre-wrap' }}>
                {c.description}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** 评测协议：数据集 / 划分 / 指标 / 样本数（只渲染存在的字段）。 */
function EvalProtocolBlock({ protocol }: { protocol: EvalProtocol }) {
  const rows: { label: string; value: string }[] = [];
  if (typeof protocol.dataset === 'string' && protocol.dataset.trim())
    rows.push({ label: tr('数据集', 'Dataset'), value: protocol.dataset.trim() });
  if (typeof protocol.split === 'string' && protocol.split.trim())
    rows.push({ label: tr('评测划分', 'Split'), value: protocol.split.trim() });
  if (typeof protocol.metric === 'string' && protocol.metric.trim())
    rows.push({ label: tr('评测指标', 'Metric'), value: protocol.metric.trim() });
  if (typeof protocol.n_examples === 'number' && Number.isFinite(protocol.n_examples))
    rows.push({ label: tr('样本数', 'Examples'), value: String(protocol.n_examples) });
  if (typeof protocol.n_samples === 'number' && Number.isFinite(protocol.n_samples))
    rows.push({ label: tr('采样次数', 'Samples'), value: String(protocol.n_samples) });
  if (rows.length === 0) return null;
  return (
    <div
      className="card card-pad"
      style={{ marginBottom: 22, display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))', gap: 16 }}
    >
      {rows.map((r) => (
        <div key={r.label} style={{ minWidth: 0 }}>
          <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 4 }}>{r.label}</div>
          <div className="mono" style={{ fontSize: 12.5, color: 'var(--text)', fontWeight: 600, wordBreak: 'break-word' }}>{r.value}</div>
        </div>
      ))}
    </div>
  );
}

/** 数据集清单。 */
function DatasetsBlock({ datasets }: { datasets: ExperimentDataset[] }) {
  return (
    <div className="card" style={{ overflow: 'hidden', marginBottom: 22 }}>
      {datasets.map((d, i) => (
        <div
          key={`${d.name}-${i}`}
          className="row gap12"
          style={{
            padding: '11px 18px',
            borderBottom: i < datasets.length - 1 ? '0.5px solid var(--border)' : 'none',
            alignItems: 'flex-start',
          }}
        >
          <Icon name="grid" size={14} style={{ color: 'var(--accent)', flexShrink: 0, marginTop: 2 }} />
          <div style={{ minWidth: 0 }}>
            <span className="mono" style={{ fontSize: 12.5, fontWeight: 650 }}>{d.name}</span>
            {d.size_hint && d.size_hint.trim() !== '' && (
              <span className="mono muted" style={{ fontSize: 10.5, marginLeft: 8 }}>{d.size_hint}</span>
            )}
            {d.purpose && d.purpose.trim() !== '' && (
              <div style={{ fontSize: 12, color: 'var(--text-2)', lineHeight: 1.5, marginTop: 3 }}>{d.purpose}</div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

/** 假设判定依据：plan 回写的 evidence 优先，缺失时从各轮 reflection 的
    hypothesis_updates 兜底取最近一轮的 evidence。 */
function hypothesisEvidence(exp: ExperimentDetail): Map<number, string> {
  const map = new Map<number, string>();
  const runs = [...(exp.runs ?? [])].sort((a, b) => a.seq - b.seq);
  for (const run of runs) {
    for (const u of run.reflection?.hypothesis_updates ?? []) {
      if (typeof u.index === 'number' && u.evidence && u.evidence.trim() !== '') {
        map.set(u.index, u.evidence);
      }
    }
  }
  (exp.plan?.hypotheses ?? []).forEach((h, i) => {
    if (h.evidence && h.evidence.trim() !== '') map.set(i, h.evidence);
  });
  return map;
}

/** 单条假设：文案 + 实时状态 chip（testing 灰 / verified 绿 / falsified 红），
    有判定依据时可折叠展开（chip 上也有 tooltip）。 */
function HypRow({
  hyp,
  index,
  evidence,
}: {
  hyp: NonNullable<ExperimentPlan['hypotheses']>[number];
  index: number;
  evidence: string | undefined;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="card" style={{ padding: '12px 16px' }}>
      <div className="row gap12">
        <span className="mono" style={{ fontSize: 11, color: 'var(--text-4)', flexShrink: 0 }}>H{index + 1}</span>
        <span style={{ fontSize: 13, flex: 1, lineHeight: 1.5 }}>{hyp.text}</span>
        {evidence && (
          <button
            className="btn btn-ghost sm"
            title={evidence}
            style={{ flexShrink: 0 }}
            onClick={() => setOpen((o) => !o)}
          >
            {tr('依据', 'Evidence')}
            <Icon
              name="chevDown"
              size={11}
              style={{ transform: open ? 'rotate(180deg)' : 'none', transition: 'transform .15s' }}
            />
          </button>
        )}
        <HypChip status={hyp.status} title={evidence} />
      </div>
      {open && evidence && (
        <div
          style={{
            marginTop: 9,
            padding: '9px 12px',
            borderRadius: 8,
            background: 'var(--surface-2)',
            fontSize: 12,
            lineHeight: 1.6,
            color: 'var(--text-2)',
            whiteSpace: 'pre-wrap',
          }}
        >
          {tr('判定依据：', 'Evidence: ')}{evidence}
        </div>
      )}
    </div>
  );
}

function PlanTab({ exp, onOpenGates }: { exp: ExperimentDetail; onOpenGates: () => void }) {
  const plan = exp.plan;
  const evidence = hypothesisEvidence(exp);

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
          {tr(
            '实验已暂停，等待算力预算审批：消耗真实算力前需人工确认方案与预算。',
            'Experiment paused for compute budget approval: the plan and budget need a human sign-off before real compute is spent.',
          )}
          <button className="btn btn-primary sm" style={{ marginLeft: 'auto' }} onClick={onOpenGates}>
            {tr('前往审批', 'Open approvals')}
          </button>
        </div>
      )}

      {!plan ? (
        <div className="card">
          <EmptyState
            compact
            icon="sparkle"
            title={exp.status === 'planning' ? tr('计划生成中…', 'Generating the plan…') : tr('计划尚未生成', 'Plan not generated yet')}
            desc={tr(
              '系统读入想法内容与相关文献后自动产出假设清单、复现策略与预算估计。',
              'The system reads the idea and related papers, then produces hypotheses, a repro strategy and a budget estimate.',
            )}
          />
        </div>
      ) : (
        <>
          {/* 概览：实验类型 + 主指标 + 运行环境 */}
          <PlanOverview exp={exp} />

          {/* 假设清单 */}
          <span className="section-h" style={{ marginBottom: 12 }}>
            <Icon name="sparkle" size={15} style={{ color: 'var(--accent)' }} />
            {tr('假设清单', 'Hypotheses')}
          </span>
          {(plan.hypotheses ?? []).length === 0 ? (
            <div className="card empty" style={{ padding: 24, marginBottom: 22 }}>{tr('计划中未包含假设清单', 'The plan has no hypotheses')}</div>
          ) : (
            <div className="col gap8" style={{ marginBottom: 22 }}>
              {(plan.hypotheses ?? []).map((h, i) => (
                <HypRow key={i} hyp={h} index={i} evidence={evidence.get(i)} />
              ))}
            </div>
          )}

          {/* 对照设置：baseline vs treatment（单一配置实验无此块） */}
          {(plan.conditions ?? []).length > 0 && (
            <>
              <span className="section-h" style={{ marginBottom: 12 }}>
                <Icon name="scale" size={15} style={{ color: 'var(--accent)' }} />
                {tr('对照设置', 'Conditions')}
              </span>
              <ConditionsBlock conditions={plan.conditions ?? []} />
            </>
          )}

          {/* 评测协议：数据集 / 划分 / 指标 / 样本数 */}
          {plan.eval_protocol && (
            <>
              <span className="section-h" style={{ marginBottom: 12 }}>
                <Icon name="sliders" size={15} style={{ color: 'var(--accent)' }} />
                {tr('评测协议', 'Eval protocol')}
              </span>
              <EvalProtocolBlock protocol={plan.eval_protocol} />
            </>
          )}

          {/* 数据集 */}
          {(plan.datasets ?? []).length > 0 && (
            <>
              <span className="section-h" style={{ marginBottom: 12 }}>
                <Icon name="grid" size={15} style={{ color: 'var(--accent)' }} />
                {tr('数据集', 'Datasets')}
              </span>
              <DatasetsBlock datasets={plan.datasets ?? []} />
            </>
          )}

          {/* 复现策略 */}
          <span className="section-h" style={{ marginBottom: 12 }}>
            <Icon name="layers" size={15} style={{ color: 'var(--accent)' }} />
            {tr('复现策略', 'Repro strategy')}
          </span>
          <div className="card card-pad" style={{ marginBottom: 22, background: 'var(--surface-2)' }}>
            <p style={{ fontSize: 13, lineHeight: 1.65, margin: 0, color: 'var(--text-2)', whiteSpace: 'pre-wrap' }}>
              {plan.repro_strategy || tr('（计划中未包含复现策略）', '(the plan has no repro strategy)')}
            </p>
          </div>

          {/* 实验步骤 */}
          <span className="section-h" style={{ marginBottom: 12 }}>
            <Icon name="compass" size={15} style={{ color: 'var(--accent)' }} />
            {tr('实验步骤', 'Steps')}
          </span>
          {(plan.steps ?? []).length === 0 ? (
            <div className="card empty" style={{ padding: 24, marginBottom: 22 }}>{tr('计划中未包含步骤列表', 'The plan has no step list')}</div>
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
            {tr('预算', 'Budget')}
          </span>
          <div className="row gap12" style={{ alignItems: 'stretch' }}>
            <div className="card card-pad" style={{ flex: 1 }}>
              <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 6 }}>{tr('硬上限（超限自动 kill）', 'Hard limits (auto-killed when exceeded)')}</div>
              <div className="mono" style={{ fontSize: 18, fontWeight: 700 }}>{budgetText(exp.budget)}</div>
            </div>
            <div className="card card-pad" style={{ flex: 1.6, background: 'var(--surface-2)' }}>
              <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 6 }}>{tr('计划估计', 'Planned estimate')}</div>
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
          {file.content || tr('（空文件）', '(empty file)')}
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
  // 实验所在服务器的系统状态：搭建/运行期间实时刷新（20s）
  const sysActive = !EXPERIMENT_TERMINAL.has(exp.status);
  const sysinfo = useQuery({
    queryKey: ['experiment', exp.id, 'sysinfo'],
    queryFn: () => api.getExperimentSysinfo(exp.id),
    retry: false,
    refetchInterval: sysActive ? 20_000 : false,
  });
  const sysinfoCard = (
    <div className="card card-pad" style={{ marginBottom: 18 }}>
      <span className="section-h" style={{ marginBottom: 10 }}>
        <Icon name="server" size={15} style={{ color: 'var(--accent)' }} />
        {tr('服务器状态', 'Server status')}
        {exp.server_host && <span className="mono muted" style={{ fontSize: 11 }}>{exp.server_host}</span>}
      </span>
      <SysinfoPanel
        loading={sysinfo.isLoading}
        error={sysinfo.isError}
        info={sysinfo.data}
        onRefresh={() => void sysinfo.refetch()}
      />
    </div>
  );

  if (!vid) {
    return (
      <div className="card">
        <EmptyState
          compact
          icon="server"
          title={tr('尚未关联任务', 'No linked task yet')}
          desc={tr('实验创建后会入队一个 kind=experiment 的 voyage。', 'Creating an experiment queues a kind=experiment voyage.')}
        />
      </div>
    );
  }
  if (isLoading) return <div className="empty" style={{ padding: 40 }}>{tr('加载环境搭建步骤…', 'Loading setup steps…')}</div>;
  if (isError || !voyage) {
    return (
      <div className="card">
        <EmptyState
          compact
          icon="x"
          title={tr('无法加载关联任务', 'Could not load the linked task')}
          desc={tr('后端不可用或接口尚未就绪。', 'Backend unavailable or the API is not ready yet.')}
        />
      </div>
    );
  }

  const all = [...(voyage.steps ?? [])].sort((a, b) => a.seq - b.seq);
  const matched = all.filter((s) => SETUP_STEP_RE.test(`${s.action} ${s.title}`));
  const steps = matched.length > 0 ? matched : all;
  const files = extractFiles(all);

  return (
    <div className="fadeup" style={{ maxWidth: 860 }}>
      {sysinfoCard}
      <div className="row gap8" style={{ marginBottom: 12, justifyContent: 'space-between' }}>
        <span className="section-h">
          <Icon name="server" size={15} style={{ color: 'var(--accent)' }} />
          {tr('环境搭建步骤', 'Setup steps')} <span className="en-label" style={{ fontSize: 11 }}>{tr('来自关联任务', 'from the linked task')}</span>
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
        <div className="card empty" style={{ padding: 32, marginBottom: 24 }}>{tr('任务尚未产生步骤', 'The task has no steps yet')}</div>
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
                        {fmtTime(s.started_at)} · {s.finished_at ? `${tr('耗时', 'took')} ${fmtDuration(s.started_at, s.finished_at)}` : tr('进行中', 'in progress')}
                      </div>
                    )}
                    {s.verdict && !s.verdict.passed && s.verdict.reason && (
                      <div style={{ marginTop: 7, fontSize: 12, color: 'var(--danger-tx)', lineHeight: 1.5 }}>
                        {tr('自动校验：', 'Auto check: ')}{s.verdict.reason}
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
        {tr('生成的代码文件', 'Generated code files')} <span className="en-label" style={{ fontSize: 11 }}>{files.length}</span>
      </span>
      {files.length === 0 ? (
        <div className="card empty" style={{ padding: 28 }}>
          {tr(
            '暂无生成文件（建环境阶段 LLM 生成 train.py / eval.py / requirements.txt / run.sh 后显示在这里）',
            'No generated files yet (train.py / eval.py / requirements.txt / run.sh written during setup will show up here)',
          )}
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
  const figures = exp.figures ?? [];
  return (
    <div className="fadeup" style={{ maxWidth: 860 }}>
      {/* 实验图表：figures 步骤由 AI 从各轮指标数据绘制 */}
      {figures.length > 0 && (
        <>
          <span className="section-h" style={{ marginBottom: 12 }}>
            <Icon name="chart" size={15} style={{ color: 'var(--accent)' }} />
            {tr('实验图表', 'Figures')} <span className="en-label" style={{ fontSize: 11 }}>{figures.length}</span>
          </span>
          <div className="card card-pad" style={{ marginBottom: 22 }}>
            <ExperimentFigures expId={exp.id} figures={figures} />
          </div>
        </>
      )}

      {!exp.report ? (
        <div className="card">
          <EmptyState
            compact
            icon="pen"
            title={tr('报告尚未生成', 'No report yet')}
            desc={
              EXPERIMENT_TERMINAL.has(exp.status)
                ? tr('该实验未产出报告。', 'This experiment produced no report.')
                : tr('自动迭代结束后，AI 会汇总各轮指标、图表与日志生成 markdown 报告。', 'After the auto-iteration finishes, the AI summarizes metrics, figures and logs into a markdown report.')
            }
          />
        </div>
      ) : (
        <div className="card card-pad">
          <Markdown source={exp.report} />
        </div>
      )}
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
      toast(tr('实验已取消', 'Experiment cancelled'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['experiment', id] });
      void queryClient.invalidateQueries({ queryKey: ['experiments'] });
      void queryClient.invalidateQueries({ queryKey: ['voyages'] });
    },
    onError: (e) => toast(`${tr('取消失败：', 'Cancel failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  if (isLoading) {
    return (
      <div className="page fadeup">
        <div className="empty" style={{ padding: 80 }}>{tr('加载实验详情…', 'Loading experiment…')}</div>
      </div>
    );
  }
  if (isError || !exp) {
    const notFound = error instanceof ApiError && error.status === 404;
    return (
      <div className="page fadeup">
        <div className="card card-pad" style={{ textAlign: 'center', padding: 60 }}>
          <div style={{ fontSize: 15, fontWeight: 650, marginBottom: 8 }}>
            {notFound ? tr('实验不存在', 'Experiment not found') : tr('无法加载实验详情', 'Could not load the experiment')}
          </div>
          <div style={{ fontSize: 12.5, color: 'var(--text-3)', marginBottom: 18 }}>
            {error instanceof Error ? error.message : tr('后端不可用，请稍后重试', 'Backend unavailable — try again later')}
          </div>
          <div className="row gap8" style={{ justifyContent: 'center' }}>
            <button className="btn btn-soft" onClick={() => void refetch()}>{tr('重试', 'Retry')}</button>
            <button className="btn btn-ghost" onClick={() => navigate('/experiment')}>{tr('返回列表', 'Back to list')}</button>
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
            {exp.plan && <KindBadge kind={exp.plan.kind} />}
            {exp.plan && <EnvBadge container={exp.plan.container} />}
            <span className="pill sm">
              <Icon name="server" size={11} />
              {exp.server_host ?? tr('未分配', 'Unassigned')}
            </span>
            <span className="pill sm mono" style={{ background: 'var(--surface-3)' }}>{budgetText(exp.budget)}</span>
            {exp.workdir && (
              <span className="mono muted" style={{ fontSize: 11 }}>{exp.workdir}</span>
            )}
            <span className="mono muted" style={{ fontSize: 11 }}>
              {tr('创建', 'Created')} {fmtTime(exp.created_at)} · {tr('耗时', 'took')} {fmtDuration(exp.created_at, active ? null : exp.updated_at)}
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
            {tr('查看 idea', 'View idea')}
          </button>
          {active && (
            <button
              className="btn btn-ghost"
              disabled={cancelMutation.isPending}
              onClick={() => {
                if (window.confirm(tr('确定取消该实验？将取消关联任务并尝试终止远端进程。', 'Cancel this experiment? The linked task will be cancelled and remote processes killed.'))) {
                  cancelMutation.mutate();
                }
              }}
            >
              <Icon name="x" size={13} />
              {cancelMutation.isPending ? tr('取消中…', 'Cancelling…') : tr('取消实验', 'Cancel experiment')}
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
            {tr(t.zh, t.en)}
          </button>
        ))}
      </div>

      {tab === 'plan' && <PlanTab exp={exp} onOpenGates={() => openGates(null)} />}
      {tab === 'setup' && <SetupTab exp={exp} />}
      {tab === 'run' && <RunTab exp={exp} active={active} />}
      {tab === 'code' && <CodeTab exp={exp} active={active} />}
      {tab === 'report' && <ReportTab exp={exp} />}
    </div>
  );
}
