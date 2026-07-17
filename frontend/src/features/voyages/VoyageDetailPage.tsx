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
import { tr } from '../../lib/i18n';
import {
  api,
  ApiError,
  VOYAGE_TERMINAL,
  type VoyageAcceptance,
  type VoyageAcceptanceCheck,
  type VoyageDetail,
  type VoyagePlanEvent,
  type VoyageStatus,
  type VoyageStepAttempt,
  type VoyageStepRead,
} from '../../lib/api';

/* ============================================================
   /voyages/:id — 任务详情：循环感知的活动状态 + 步骤时间线 + SSE 实时。
   体现背后 agent 的「规划 → 执行 → 校验 → 按结果调整计划」循环：
   - 顶部显示当前活动的一句话（而非线性四段进度条）与执行方式徽标；
   - 步骤卡展示验收标准、判定理由、来源（第几次调整新增）、尝试记录；
   - 时间线按 plan_history 插入「计划调整」分隔条目，解释为什么多出新步骤。
   活动状态订阅 /voyages/{id}/events，事件与 TanStack Query 缓存合并。
   ============================================================ */

/** 步骤 token 数：后端存 {prompt_tokens, completion_tokens} 字典，历史数据可能是数字。 */
function stepTokenCount(tokens: VoyageStepRead['tokens']): number | null {
  if (typeof tokens === 'number') return tokens;
  if (tokens && typeof tokens === 'object') {
    return (tokens.prompt_tokens ?? 0) + (tokens.completion_tokens ?? 0);
  }
  return null;
}

function asObj(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
}

function num(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : 0;
}

// —— 顶部状态区：执行方式徽标 + 当前活动 ——

/** mode → 大白话标签与说明（模块级常量只存 zh/en 字段，渲染处再 tr）。 */
const MODE_INFO: Record<string, { zh: string; en: string; hintZh: string; hintEn: string }> = {
  pipeline: {
    zh: '固定流程',
    en: 'Fixed pipeline',
    hintZh: '步骤在创建时已完全确定，按固定顺序执行，不会中途调整计划',
    hintEn: 'Steps are fully fixed at creation and run in order; the plan never changes mid-run',
  },
  template: {
    zh: '模板流程',
    en: 'Template flow',
    hintZh: '按预设模板执行，在预设的分支点根据执行结果补充后续步骤',
    hintEn: 'Runs from a preset template; follow-up steps are added at preset branch points based on results',
  },
  loop: {
    zh: 'AI 动态规划',
    en: 'AI dynamic planning',
    hintZh: 'AI 规划步骤并循环推进：每步执行后自动校验，再按结果动态调整后续计划',
    hintEn: 'AI plans the steps and runs a loop: each step is auto-checked, then the remaining plan is adjusted based on results',
  },
};

function ModeBadge({ mode }: { mode: string }) {
  const m = MODE_INFO[mode];
  if (!m) return null;
  return (
    <span
      className="pill sm"
      style={{ background: 'var(--surface-3)', color: 'var(--text-2)', flexShrink: 0 }}
      title={tr(m.hintZh, m.hintEn)}
    >
      <Icon name={mode === 'loop' ? 'sparkle' : 'layers'} size={11} />
      {tr(m.zh, m.en)}
    </span>
  );
}

/** 从 status + cursor + steps 推导当前活动的一句话。 */
function activityText(voyage: VoyageDetail, steps: VoyageStepRead[]): string {
  const live = steps.filter((s) => s.status !== 'obsolete');
  let curIdx = live.findIndex((s) => s.status === 'running' || s.status === 'verifying');
  if (curIdx < 0 && typeof voyage.cursor === 'number' && voyage.cursor >= 0 && voyage.cursor < live.length) {
    curIdx = voyage.cursor;
  }
  const cur = curIdx >= 0 ? live[curIdx] : null;
  const stepRef = cur
    ? tr(`第 ${curIdx + 1} 步 · ${cur.title}`, `step ${curIdx + 1} · ${cur.title}`)
    : null;

  switch (voyage.status) {
    case 'planning':
      return tr('AI 正在规划步骤…', 'AI is planning the steps…');
    case 'executing': {
      if (!stepRef) return tr('正在执行步骤…', 'Executing steps…');
      const runSuffix =
        cur && cur.attempt > 1
          ? tr(`（第 ${cur.attempt} 次运行）`, ` (run ${cur.attempt})`)
          : '';
      return tr(`正在执行：${stepRef}${runSuffix}`, `Executing ${stepRef}${runSuffix}`);
    }
    case 'verifying':
      return stepRef
        ? tr(`正在校验：${stepRef}`, `Checking ${stepRef}`)
        : tr('正在校验执行结果…', 'Checking results…');
    case 'replanning':
      return tr(
        `正在调整计划（第 ${(voyage.plan_iteration ?? 0) + 1} 次）…`,
        `Adjusting the plan (adjustment ${(voyage.plan_iteration ?? 0) + 1})…`,
      );
    case 'paused_gate':
      return tr('已暂停：等待人工审批', 'Paused: waiting for approval');
    case 'paused_error':
      return tr('已暂停：执行出错', 'Paused: an error occurred');
    case 'done': {
      const passed = live.filter((s) => s.status === 'passed').length;
      const adj = voyage.plan_iteration ?? 0;
      return (
        tr(`任务完成：共执行 ${passed} 步`, `Task finished: ${passed} steps completed`) +
        (adj > 0 ? tr(`，期间计划调整 ${adj} 次`, `; the plan was adjusted ${adj} time(s)`) : '')
      );
    }
    case 'failed':
      return tr('任务失败', 'Task failed');
    case 'cancelled':
      return tr('任务已取消', 'Task cancelled');
  }
}

function activityDot(status: VoyageStatus): { color: string; pulse: boolean } {
  switch (status) {
    case 'paused_gate':
      return { color: 'var(--warn-tx)', pulse: false };
    case 'paused_error':
    case 'failed':
      return { color: 'var(--danger-tx)', pulse: false };
    case 'done':
      return { color: 'var(--ok)', pulse: false };
    case 'cancelled':
      return { color: 'var(--text-4)', pulse: false };
    default:
      return { color: 'var(--accent)', pulse: true };
  }
}

function ActivityBar({
  voyage,
  steps,
  onOpenGates,
  onResume,
  resuming,
}: {
  voyage: VoyageDetail;
  steps: VoyageStepRead[];
  onOpenGates: () => void;
  onResume?: () => void;
  resuming?: boolean;
}) {
  const status = voyage.status;
  const dot = activityDot(status);
  const live = steps.filter((s) => s.status !== 'obsolete');
  const passed = live.filter((s) => s.status === 'passed').length;
  return (
    <div>
      <div className="row gap10" style={{ flexWrap: 'wrap' }}>
        <span className={'dot' + (dot.pulse ? ' pulse' : '')} style={{ background: dot.color, flexShrink: 0 }} />
        <span style={{ fontSize: 13.5, fontWeight: 650, minWidth: 0 }}>{activityText(voyage, steps)}</span>
        <div className="row gap8" style={{ marginLeft: 'auto' }}>
          {live.length > 0 && (
            <span className="mono muted" style={{ fontSize: 11 }}>
              {tr(`已完成 ${passed}/${live.length} 步`, `${passed}/${live.length} steps done`)}
            </span>
          )}
          <ModeBadge mode={voyage.mode} />
        </div>
      </div>
      {status === 'paused_gate' && (
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
          {tr('任务已暂停，等待人工审批后继续。', 'Task paused — it will continue after approval.')}
          <button className="btn btn-primary sm" style={{ marginLeft: 'auto' }} onClick={onOpenGates}>
            {tr('前往审批', 'Go to approvals')}
          </button>
        </div>
      )}
      {status === 'paused_error' && (
        <div className="row gap8" style={{ marginTop: 12, padding: '10px 14px', background: 'var(--danger-bg)', color: 'var(--danger-tx)', borderRadius: 10, fontSize: 12.5 }}>
          <Icon name="x" size={14} />
          {tr('任务因错误暂停：暂时性故障可直接重试；若是程序问题，修复后再重试，已完成的步骤不会重跑。', 'Task paused on an error. Retry directly for transient failures; for code issues, fix first then retry — finished steps will not rerun.')}
          {onResume && (
            <button className="btn btn-primary sm" style={{ marginLeft: 'auto' }} disabled={resuming} onClick={onResume}>
              {resuming ? tr('重试中…', 'Retrying…') : tr('重试恢复', 'Retry & resume')}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// —— 论文分享 PPT（kind=presentation）：完成后下载产物 ——

function PresentationDownload({ voyageId, goal }: { voyageId: string; goal: string }) {
  const downloadMutation = useMutation({
    mutationFn: () => api.downloadPresentation(voyageId),
    onSuccess: (blob) => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${goal.replace(/[/\\:*?"<>|]/g, ' ').slice(0, 60)}.pptx`;
      a.click();
      URL.revokeObjectURL(url);
    },
    onError: (e) =>
      toast(
        e instanceof ApiError && e.message === 'FILE_NOT_READY'
          ? tr('文件还没生成好，稍后再试', 'File not ready yet — try again shortly')
          : `${tr('下载失败：', 'Download failed: ')}${e instanceof Error ? e.message : String(e)}`,
        'error',
      ),
  });
  return (
    <button
      className="btn btn-primary"
      disabled={downloadMutation.isPending}
      onClick={() => downloadMutation.mutate()}
    >
      <Icon name="download" size={13} />
      {downloadMutation.isPending ? tr('下载中…', 'Downloading…') : tr('下载 PPT', 'Download PPT')}
    </button>
  );
}

// —— 文献任务（wiki_bootstrap / wiki_ingest）：observation 的用户可读摘要 ——

const WIKI_RUN_KINDS = new Set(['wiki_bootstrap', 'wiki_ingest']);

interface PaperBrief {
  id: string;
  title: string;
  score?: number;
  passed?: boolean;
  pdf?: boolean;
}

function briefsOf(v: unknown): PaperBrief[] {
  if (!Array.isArray(v)) return [];
  return v.filter(
    (x): x is PaperBrief => !!x && typeof x === 'object' && typeof (x as PaperBrief).title === 'string',
  );
}

function namesOf(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [];
}

interface WikiStepFriendly {
  /** 一句给用户看的中文小结 */
  text: string;
  /** 涉及的论文清单（可点击跳文献库） */
  papers: PaperBrief[];
  /** 新概念名（wiki.link_concepts） */
  concepts?: string[];
}

/** 把 wiki.* 动作的 observation 翻译成用户可读摘要；不认识的动作返回 null（回退原始 JSON）。 */
function wikiStepFriendly(action: string, obs: Record<string, unknown>): WikiStepFriendly | null {
  const failedCount = Array.isArray(obs.failed) ? obs.failed.length : 0;
  switch (action) {
    case 'wiki.search_candidates':
      return {
        text: tr(
          `从 arXiv 检索到 ${num(obs.found)} 篇，去重后新收录 ${num(obs.inserted)} 篇`,
          `Found ${num(obs.found)} papers on arXiv; ${num(obs.inserted)} new after dedup`,
        ),
        papers: briefsOf(obs.new_papers),
      };
    case 'wiki.snowball':
      if (obs.skipped) return { text: tr('已跳过（未开启参考文献扩展）', 'Skipped (reference expansion is off)'), papers: [] };
      return {
        text:
          tr(
            `顺着 ${num(obs.processed)} 篇种子论文的参考文献与引用扩展，新收录 ${num(obs.inserted)} 篇`,
            `Expanded references and citations of ${num(obs.processed)} seed papers; ${num(obs.inserted)} new papers added`,
          ) +
          (failedCount ? tr(`（${failedCount} 篇种子查询失败）`, ` (${failedCount} seed lookups failed)`) : ''),
        papers: briefsOf(obs.new_papers),
      };
    case 'wiki.score_relevance': {
      const passed = num(obs.succeeded) - num(obs.excluded);
      return {
        text:
          tr(
            `AI 按研究方向给 ${num(obs.processed)} 篇候选论文打了相关性分：${passed} 篇通过，${num(obs.excluded)} 篇相关性不足自动删除`,
            `AI scored ${num(obs.processed)} candidate papers against the research direction: ${passed} passed, ${num(obs.excluded)} removed as not relevant enough`,
          ) +
          (failedCount ? tr(`，${failedCount} 篇打分失败`, `; ${failedCount} failed to score`) : ''),
        papers: briefsOf(obs.scored_papers),
      };
    }
    case 'wiki.fetch_extract':
      return {
        text:
          tr(
            `为 ${num(obs.processed)} 篇高分论文下载 PDF 并提取全文`,
            `Downloaded PDFs and extracted full text for ${num(obs.processed)} high-scoring papers`,
          ) +
          (num(obs.degraded)
            ? tr(`，${num(obs.degraded)} 篇没拿到原文（后续用摘要代替）`, `; ${num(obs.degraded)} had no full text (abstract used instead)`)
            : ''),
        papers: briefsOf(obs.fetched_papers),
      };
    case 'wiki.compile':
      return {
        text:
          tr(`AI 精读并编译了 ${num(obs.succeeded)} 篇论文介绍`, `AI read and compiled intros for ${num(obs.succeeded)} papers`) +
          (failedCount ? tr(`，${failedCount} 篇失败（下次同步会重试）`, `; ${failedCount} failed (will retry next sync)`) : ''),
        papers: briefsOf(obs.compiled_papers),
      };
    case 'wiki.link_concepts':
      return {
        text: tr(
          `从编译的介绍中整理概念：新增 ${num(obs.concepts_created)} 个概念，建立 ${num(obs.links_created)} 条论文—概念关联`,
          `Organized concepts from the compiled intros: ${num(obs.concepts_created)} new concepts, ${num(obs.links_created)} paper–concept links`,
        ),
        papers: [],
        concepts: namesOf(obs.new_concepts),
      };
    case 'wiki.update_watermark':
      return {
        text: obs.watermark
          ? tr(
              `已记录本次同步时间，下次增量同步从 ${String(obs.watermark).slice(0, 10)} 附近继续`,
              `Sync time recorded — the next incremental sync resumes from around ${String(obs.watermark).slice(0, 10)}`,
            )
          : tr('已记录本次同步时间', 'Sync time recorded'),
        papers: [],
      };
    default:
      return null;
  }
}

const PAPER_LIST_PREVIEW = 5;

function StepPaperList({ papers }: { papers: PaperBrief[] }) {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  if (papers.length === 0) return null;
  const shown = open ? papers : papers.slice(0, PAPER_LIST_PREVIEW);
  return (
    <div className="col" style={{ gap: 3, marginTop: 8 }}>
      {shown.map((p) => (
        <div key={p.id} className="row gap6" style={{ fontSize: 12, minWidth: 0 }}>
          {p.passed === false ? (
            <Icon name="x" size={11} style={{ color: 'var(--text-4)', flexShrink: 0 }} />
          ) : (
            <Icon name="book" size={11} style={{ color: 'var(--text-3)', flexShrink: 0 }} />
          )}
          <span
            style={{
              cursor: 'pointer',
              color: p.passed === false ? 'var(--text-4)' : 'var(--text-2)',
              textDecoration: p.passed === false ? 'line-through' : 'none',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              minWidth: 0,
            }}
            title={p.title}
            onClick={() => navigate(`/wiki?paper=${p.id}`)}
          >
            {p.title}
          </span>
          {typeof p.score === 'number' && (
            <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', flexShrink: 0 }}>
              {p.score.toFixed(2)}
            </span>
          )}
          {p.pdf === false && (
            <span className="mono" style={{ fontSize: 10, color: 'var(--warn-tx)', flexShrink: 0 }}>{tr('无 PDF', 'no PDF')}</span>
          )}
        </div>
      ))}
      {papers.length > PAPER_LIST_PREVIEW && (
        <button
          onClick={() => setOpen(!open)}
          style={{ border: 'none', background: 'transparent', cursor: 'pointer', padding: 0, fontSize: 11.5, color: 'var(--accent-text)', textAlign: 'left' }}
        >
          {open ? tr('收起', 'Collapse') : tr(`展开全部 ${papers.length} 篇`, `Show all ${papers.length} papers`)}
        </button>
      )}
    </div>
  );
}

function WikiStepSummary({ friendly }: { friendly: WikiStepFriendly }) {
  return (
    <div
      style={{
        marginTop: 10,
        padding: '9px 12px',
        background: 'var(--surface-2)',
        borderRadius: 9,
        fontSize: 12.5,
        lineHeight: 1.6,
        color: 'var(--text)',
      }}
    >
      {friendly.text}
      <StepPaperList papers={friendly.papers} />
      {friendly.concepts && friendly.concepts.length > 0 && (
        <div className="row gap6 wrap" style={{ marginTop: 8 }}>
          {friendly.concepts.map((name) => (
            <span key={name} className="tag" style={{ fontSize: 10.5 }}>
              {name}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/** 文献任务的整体结果卡：从各步 observation 汇总本次新增/编译数量。 */
function WikiRunSummary({ steps }: { steps: VoyageStepRead[] }) {
  const obsOf = (action: string) =>
    asObj(steps.find((s) => s.action === action && s.status === 'passed')?.observation);
  const search = obsOf('wiki.search_candidates');
  const snowball = obsOf('wiki.snowball');
  const score = obsOf('wiki.score_relevance');
  const compile = obsOf('wiki.compile');
  const link = obsOf('wiki.link_concepts');
  if (!search && !compile) return null;

  const stats: { label: string; value: number }[] = [];
  if (search || snowball) stats.push({ label: tr('新收录论文', 'New papers'), value: num(search?.inserted) + num(snowball?.inserted) });
  if (score) stats.push({ label: tr('通过筛选', 'Passed screening'), value: num(score.succeeded) - num(score.excluded) });
  if (compile) stats.push({ label: tr('已编译', 'Compiled'), value: num(compile.succeeded) });
  if (link) stats.push({ label: tr('新增概念', 'New concepts'), value: num(link.concepts_created) });
  if (stats.length === 0) return null;

  return (
    <div className="card card-pad" style={{ marginBottom: 20 }}>
      <div className="row" style={{ marginBottom: 10 }}>
        <span className="section-h">
          <Icon name="book" size={15} style={{ color: 'var(--accent)' }} />
          {tr('本次同步结果', 'Sync summary')}
        </span>
      </div>
      <div className="row" style={{ gap: 28, flexWrap: 'wrap' }}>
        {stats.map((s) => (
          <div key={s.label}>
            <div className="mono" style={{ fontSize: 22, fontWeight: 680, lineHeight: 1.2 }}>{s.value}</div>
            <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 2 }}>{s.label}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

// —— 验收标准 / 判定 ——

/** 结构化检查项 → 大白话（未知 kind 原样展示）。 */
function checkText(c: VoyageAcceptanceCheck): string {
  switch (c.kind) {
    case 'no_error':
      return tr('执行无报错', 'Runs without errors');
    case 'exit_code':
      return tr(`退出码为 ${String(c.value ?? 0)}`, `Exit code is ${String(c.value ?? 0)}`);
    case 'artifact_exists':
      return tr(`产物 ${String(c.key ?? '')} 已生成`, `Artifact ${String(c.key ?? '')} exists`);
    case 'schema_valid': {
      const keys = Array.isArray(c.required_keys) ? c.required_keys.join(', ') : '';
      return tr(
        `${String(c.field ?? '')} 结构完整${keys ? `（需包含 ${keys}）` : ''}`,
        `${String(c.field ?? '')} has a valid structure${keys ? ` (must include ${keys})` : ''}`,
      );
    }
    case 'metric':
      return tr(
        `指标 ${String(c.name ?? '')} ${String(c.op ?? '')} ${String(c.value ?? '')}`,
        `Metric ${String(c.name ?? '')} ${String(c.op ?? '')} ${String(c.value ?? '')}`,
      );
    case 'min_count':
      return tr(
        `${String(c.field ?? '')} 数量 ≥ ${String(c.value ?? '')}`,
        `${String(c.field ?? '')} count ≥ ${String(c.value ?? '')}`,
      );
    case 'llm_rubric':
      return tr(`AI 按标准评审：${String(c.rubric ?? '')}`, `AI reviews against the rubric: ${String(c.rubric ?? '')}`);
    default:
      return c.kind;
  }
}

/** 验收标准区：这一步"怎样算通过"，默认收起。 */
function AcceptanceBlock({ acceptance }: { acceptance: VoyageAcceptance }) {
  const [open, setOpen] = useState(false);
  const checks = acceptance.checks ?? [];
  if (checks.length === 0 && !acceptance.text) return null;
  return (
    <div style={{ marginTop: 8 }}>
      <button
        className="row gap6"
        onClick={() => setOpen(!open)}
        style={{ border: 'none', background: 'transparent', cursor: 'pointer', padding: 0, fontSize: 11.5, fontWeight: 600, color: 'var(--text-3)' }}
      >
        <Icon name="chevDown" size={12} style={{ transform: open ? 'rotate(180deg)' : 'none', transition: 'transform .15s' }} />
        {checks.length > 0
          ? tr(`验收标准（${checks.length} 项）`, `Pass criteria (${checks.length})`)
          : tr('验收标准', 'Pass criteria')}
      </button>
      {open && (
        <div style={{ marginTop: 6, padding: '8px 12px', background: 'var(--surface-2)', borderRadius: 8, fontSize: 12, lineHeight: 1.7, color: 'var(--text-2)' }}>
          {checks.map((c, i) => (
            <div key={i} className="row gap6" style={{ alignItems: 'flex-start' }}>
              <Icon name="check" size={11} style={{ color: 'var(--text-4)', flexShrink: 0, marginTop: 4 }} />
              <span style={{ minWidth: 0 }}>{checkText(c)}</span>
            </div>
          ))}
          {acceptance.text && (
            <div style={{ marginTop: checks.length > 0 ? 6 : 0, color: 'var(--text-3)', fontSize: 11.5 }}>
              {acceptance.text}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// —— 计划调整（plan_history）——

/** source → 大白话（模块级常量只存 zh/en，渲染处再 tr）。 */
const PLAN_SOURCE: Record<string, { zh: string; en: string }> = {
  signal: { zh: '按执行结果自动调整', en: 'Auto-adjusted by results' },
  navigator: { zh: 'AI 调整计划', en: 'AI adjusted the plan' },
  template: { zh: '按预设分支调整', en: 'Preset branch adjustment' },
};

/** 时间线里的「计划调整」分隔条目：解释为什么多出/作废了步骤。 */
function PlanEventCard({ event }: { event: VoyagePlanEvent }) {
  const src = PLAN_SOURCE[event.source];
  return (
    <div
      style={{
        padding: '10px 14px',
        background: 'var(--accent-soft)',
        borderRadius: 10,
        fontSize: 12.5,
        lineHeight: 1.6,
      }}
    >
      <div className="row gap8" style={{ flexWrap: 'wrap' }}>
        <span style={{ fontWeight: 650, color: 'var(--accent-text)' }}>
          <Icon name="refresh" size={12} style={{ display: 'inline-block', verticalAlign: '-1.5px', marginRight: 5 }} />
          {tr(`计划调整 #${event.iteration}`, `Plan adjustment #${event.iteration}`)}
        </span>
        <span className="pill sm" style={{ background: 'var(--surface)', color: 'var(--text-2)' }}>
          {src ? tr(src.zh, src.en) : event.source}
        </span>
        {event.at && <span className="mono muted" style={{ fontSize: 10.5, marginLeft: 'auto' }}>{fmtTime(event.at)}</span>}
      </div>
      {event.reason && <div style={{ marginTop: 4, color: 'var(--text)' }}>{event.reason}</div>}
      <div className="row gap10" style={{ marginTop: 4, flexWrap: 'wrap', fontSize: 11, color: 'var(--text-3)' }}>
        {event.added > 0 && <span>{tr(`新增 ${event.added} 步`, `${event.added} step(s) added`)}</span>}
        {event.obsoleted > 0 && <span>{tr(`作废 ${event.obsoleted} 步`, `${event.obsoleted} step(s) dropped`)}</span>}
        {event.trigger_step && (
          <span>{tr(`由「${event.trigger_step}」触发`, `Triggered by “${event.trigger_step}”`)}</span>
        )}
      </div>
    </div>
  );
}

/** 作废步骤的原因：找作废它的那次计划调整（iteration 大于其创建轮次且最接近的一条）。 */
function obsoleteReasonOf(step: VoyageStepRead, events: VoyagePlanEvent[]): string {
  const created = step.provenance?.plan_iteration ?? 0;
  const ev = events
    .filter((e) => e.iteration > created && e.obsoleted > 0)
    .sort((a, b) => a.iteration - b.iteration)[0];
  return ev?.reason
    ? tr(`已作废：${ev.reason}`, `Dropped: ${ev.reason}`)
    : tr('已作废：计划调整时被替换', 'Dropped: replaced during a plan adjustment');
}

// —— analyze 步骤的因果摘要（observation.plan_signal）——

/** stopped_reason → 大白话（未收录原样展示）。 */
const STOPPED_REASON: Record<string, { zh: string; en: string }> = {
  no_improve: { zh: '连续无提升', en: 'no improvement across rounds' },
  max_runs: { zh: '达到轮次上限', en: 'hit the round limit' },
  max_hours: { zh: '达到时长上限', en: 'hit the time limit' },
  debug_limit: { zh: '修复次数用尽', en: 'debug attempts exhausted' },
  hypotheses_resolved: { zh: '假设已全部有结论', en: 'all hypotheses resolved' },
};

/** plan_signal → 一句因果摘要（为什么后面多了/没多新步骤）。 */
function planSignalText(sig: Record<string, unknown>): string | null {
  if (sig.decision === 'continue') {
    const nr = num(sig.next_round);
    return nr > 0
      ? tr(`分析判定：继续迭代 → 已追加第 ${nr} 轮`, `Analysis verdict: keep iterating → round ${nr} appended`)
      : tr('分析判定：继续迭代 → 已追加下一轮', 'Analysis verdict: keep iterating → next round appended');
  }
  if (sig.decision === 'finish') {
    const raw = typeof sig.stopped_reason === 'string' ? sig.stopped_reason : '';
    const m = STOPPED_REASON[raw];
    const reasonZh = m ? m.zh : raw;
    const reasonEn = m ? m.en : raw;
    return tr(
      `判定迭代结束${reasonZh ? `（${reasonZh}）` : ''} → 进入图表与报告`,
      `Iteration finished${reasonEn ? ` (${reasonEn})` : ''} → moving on to figures & report`,
    );
  }
  return null;
}

// —— 尝试记录（attempts 归档）——

function AttemptsBlock({ attempts }: { attempts: VoyageStepAttempt[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ marginTop: 8 }}>
      <button
        className="row gap6"
        onClick={() => setOpen(!open)}
        style={{ border: 'none', background: 'transparent', cursor: 'pointer', padding: 0, fontSize: 11.5, fontWeight: 600, color: 'var(--text-3)' }}
      >
        <Icon name="chevDown" size={12} style={{ transform: open ? 'rotate(180deg)' : 'none', transition: 'transform .15s' }} />
        {open ? tr('收起尝试记录', 'Hide run history') : tr(`查看 ${attempts.length} 次尝试记录`, `Show ${attempts.length} runs`)}
      </button>
      {open && (
        <div className="col" style={{ gap: 6, marginTop: 6 }}>
          {attempts.map((a) => (
            <div key={a.attempt} style={{ padding: '7px 12px', background: 'var(--surface-2)', borderRadius: 8, fontSize: 11.5, lineHeight: 1.6 }}>
              <div className="row gap8" style={{ flexWrap: 'wrap' }}>
                <span className="mono" style={{ fontWeight: 650 }}>#{a.attempt}</span>
                {a.started_at && (
                  <span className="mono muted" style={{ fontSize: 10.5 }}>
                    {fmtTime(a.started_at)}
                    {a.finished_at ? ` – ${fmtTime(a.finished_at)} · ${fmtDuration(a.started_at, a.finished_at)}` : ''}
                  </span>
                )}
                {a.verdict && (
                  <span
                    className="pill sm"
                    style={
                      a.verdict.passed
                        ? { background: 'var(--ok-bg)', color: 'var(--ok-tx)', marginLeft: 'auto' }
                        : { background: 'var(--danger-bg)', color: 'var(--danger-tx)', marginLeft: 'auto' }
                    }
                  >
                    {a.verdict.passed ? tr('通过', 'Passed') : tr('未通过', 'Failed')}
                  </span>
                )}
              </div>
              {a.verdict?.reason && (
                <div style={{ marginTop: 3, color: a.verdict.passed ? 'var(--text-3)' : 'var(--danger-tx)' }}>
                  {a.verdict.reason}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// —— 步骤卡 ——

/** 需审批的类型 → 大白话（未知 kind 原样展示）。 */
const GATE_KIND: Record<string, { zh: string; en: string }> = {
  compute_budget: { zh: '算力预算', en: 'compute budget' },
  experiment_pivot: { zh: '方法调整', en: 'method pivot' },
};

function stepMarker(step: VoyageStepRead): { bg: string; color: string } {
  if (step.status === 'obsolete') return { bg: 'var(--surface-3)', color: 'var(--text-4)' };
  if (step.verdict && !step.verdict.passed) return { bg: 'var(--danger-bg)', color: 'var(--danger-tx)' };
  switch (step.status) {
    case 'passed':
      return { bg: 'var(--ok-bg)', color: 'var(--ok-tx)' };
    case 'running':
    case 'verifying':
      return { bg: 'var(--accent)', color: '#fff' };
    case 'failed':
      return { bg: 'var(--danger-bg)', color: 'var(--danger-tx)' };
    default:
      return { bg: 'var(--surface-2)', color: 'var(--text-3)' };
  }
}

/** 清单序 = 执行序：按 rank 排（计划调整的插入节点 rank 取间隙值），seq 只是创建序。 */
function byListOrder(a: VoyageStepRead, b: VoyageStepRead): number {
  return (a.rank ?? 0) - (b.rank ?? 0) || a.seq - b.seq;
}

function ObservationBlock({ observation, compact }: { observation: unknown; compact?: boolean }) {
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
        {compact ? tr('原始数据', 'Raw data') : tr('运行结果数据', 'Result data')}
      </button>
      {/* 有可读摘要（compact）时原始 JSON 只在展开后出现 */}
      {(open || !compact) && (
        <div className="codeblock scroll" style={{ fontSize: 11, marginTop: 6, maxHeight: open ? 400 : 'none', overflowY: 'auto', whiteSpace: 'pre-wrap' }}>
          {open ? text : preview}
        </div>
      )}
    </div>
  );
}

function StepCard({ step, planEvents }: { step: VoyageStepRead; planEvents: VoyagePlanEvent[] }) {
  const obs = asObj(step.observation);
  const friendly = obs ? wikiStepFriendly(step.action, obs) : null;
  const obsolete = step.status === 'obsolete';
  const planIter = step.provenance?.plan_iteration ?? 0;
  const gateKind = step.requires_gate ? GATE_KIND[step.requires_gate] : null;
  const signal = obs ? asObj(obs.plan_signal) : null;
  const signalText = signal ? planSignalText(signal) : null;
  const attempts = step.attempts ?? [];
  return (
    <div className="card" style={{ padding: '14px 16px', opacity: obsolete ? 0.55 : 1 }}>
      <div className="row gap8" style={{ flexWrap: 'wrap' }}>
        <span style={{ fontSize: 13.5, fontWeight: 650, textDecoration: obsolete ? 'line-through' : 'none' }}>
          {step.title}
        </span>
        <span className="tag mono" style={{ fontSize: 10.5 }}>{step.action}</span>
        {planIter > 0 && (
          <span
            className="pill sm"
            style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}
            title={tr('这一步不在初始计划里，是后来计划调整时新增的', 'Not in the initial plan — added by a later plan adjustment')}
          >
            {tr(`第 ${planIter} 次调整新增`, `Added in adjustment ${planIter}`)}
          </span>
        )}
        {step.requires_gate && (
          <span
            className="pill sm"
            style={{ background: 'var(--warn-bg)', color: 'var(--warn-tx)' }}
            title={tr('执行到这一步会暂停，等人工审批通过后继续', 'The task pauses here until a human approves')}
          >
            <Icon name="gate" size={11} />
            {tr(`需审批：${gateKind ? gateKind.zh : step.requires_gate}`, `Needs approval: ${gateKind ? gateKind.en : step.requires_gate}`)}
          </span>
        )}
        {step.attempt > 1 && (
          <span className="pill sm" style={{ background: 'var(--warn-bg)', color: 'var(--warn-tx)' }} title={tr('出错后带诊断自动重试过', 'Auto-retried with diagnostics after an error')}>
            {tr(`第 ${step.attempt} 次尝试`, `Attempt ${step.attempt}`)}
          </span>
        )}
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
            {step.verdict.passed ? tr('自动校验通过', 'Auto-check passed') : tr('自动校验未通过', 'Auto-check failed')}
          </span>
        )}
        {stepTokenCount(step.tokens) !== null && (
          <span className="mono muted" style={{ fontSize: 11 }}>
            <Icon name="cpu" size={11} style={{ display: 'inline-block', verticalAlign: '-1.5px', marginRight: 4 }} />
            {fmtTokens(stepTokenCount(step.tokens))} tok
          </span>
        )}
        {step.started_at && (
          <span className="mono muted" style={{ fontSize: 11 }}>
            {fmtTime(step.started_at)} · {step.finished_at ? `${tr('耗时', 'took')} ${fmtDuration(step.started_at, step.finished_at)}` : tr('进行中', 'in progress')}
          </span>
        )}
      </div>
      {/* 判定理由：通过与否都展示（通过时用弱色） */}
      {step.verdict?.reason && (
        <div style={{ marginTop: 8, fontSize: 12, color: step.verdict.passed ? 'var(--text-3)' : 'var(--danger-tx)', lineHeight: 1.5 }}>
          {tr('判定理由：', 'Verdict: ')}
          {step.verdict.reason}
        </div>
      )}
      {/* 作废步骤：补一行为什么被作废 */}
      {obsolete && (
        <div style={{ marginTop: 8, fontSize: 12, color: 'var(--text-4)', lineHeight: 1.5 }}>
          {obsoleteReasonOf(step, planEvents)}
        </div>
      )}
      {/* 分析步骤的因果摘要：这一步的结论如何改变了后续计划 */}
      {signalText && (
        <div
          className="row gap6"
          style={{ marginTop: 10, padding: '8px 12px', background: 'var(--accent-soft)', color: 'var(--accent-text)', borderRadius: 8, fontSize: 12.5, lineHeight: 1.5, alignItems: 'flex-start' }}
        >
          <Icon name="compass" size={13} style={{ flexShrink: 0, marginTop: 2 }} />
          <span style={{ minWidth: 0 }}>{signalText}</span>
        </div>
      )}
      {friendly && <WikiStepSummary friendly={friendly} />}
      {step.acceptance && <AcceptanceBlock acceptance={step.acceptance} />}
      {attempts.length > 1 && <AttemptsBlock attempts={attempts} />}
      <ObservationBlock observation={step.observation} compact={!!friendly} />
    </div>
  );
}

// —— 时间线条目：步骤 + 计划调整分隔 ——

type TimelineEntry =
  | { kind: 'step'; step: VoyageStepRead; index: number }
  | { kind: 'plan'; event: VoyagePlanEvent };

/** 按 plan_history 在第一个「该次调整新增」的步骤前插入分隔条目。 */
function buildTimelineEntries(steps: VoyageStepRead[], events: VoyagePlanEvent[]): TimelineEntry[] {
  const byIteration = new Map(events.map((e) => [e.iteration, e]));
  const inserted = new Set<number>();
  const entries: TimelineEntry[] = [];
  let index = 0;
  for (const step of steps) {
    const iter = step.provenance?.plan_iteration ?? 0;
    if (iter > 0 && !inserted.has(iter) && byIteration.has(iter)) {
      inserted.add(iter);
      entries.push({ kind: 'plan', event: byIteration.get(iter)! });
    }
    entries.push({ kind: 'step', step, index: ++index });
  }
  return entries;
}

// —— 页面 ——

export function VoyageDetailPage() {
  const { id = '' } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { openGates } = useShell();
  const [logs, setLogs] = useState<string[]>([]);
  const [live, setLive] = useState(false);
  const [showObsolete, setShowObsolete] = useState(false);

  const { data: voyage, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['voyage', id, showObsolete],
    queryFn: () => api.getVoyage(id, { includeObsolete: showObsolete }),
    retry: false,
    enabled: !!id,
  });

  const resumeMutation = useMutation({
    mutationFn: () => api.resumeVoyage(id),
    onSuccess: () => {
      toast(tr('已重新入队，从断点续跑', 'Re-queued — resuming from where it stopped'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['voyage', id] });
      void queryClient.invalidateQueries({ queryKey: ['voyages'] });
    },
    onError: (err) => toast(`${tr('重试失败：', 'Retry failed: ')}${err instanceof Error ? err.message : String(err)}`, 'error'),
  });

  const cancelMutation = useMutation({
    mutationFn: () => api.cancelVoyage(id),
    onSuccess: () => {
      toast(tr('任务已取消', 'Task cancelled'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['voyage', id] });
      void queryClient.invalidateQueries({ queryKey: ['voyages'] });
    },
    onError: (err) => toast(`${tr('取消失败：', 'Cancel failed: ')}${err instanceof Error ? err.message : String(err)}`, 'error'),
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
          queryClient.setQueriesData<VoyageDetail>({ queryKey: ['voyage', id] }, (old) =>
            old ? { ...old, status: p.status, cursor: p.cursor ?? old.cursor } : old,
          );
          if (VOYAGE_TERMINAL.has(p.status)) {
            void queryClient.invalidateQueries({ queryKey: ['voyages'] });
            void queryClient.invalidateQueries({ queryKey: ['voyage', id] });
          }
        } else if (event === 'step') {
          const p = payload as { step: VoyageStepRead };
          if (!p.step) return;
          queryClient.setQueriesData<VoyageDetail>({ queryKey: ['voyage', id] }, (old) => {
            if (!old) return old;
            const steps = old.steps ?? [];
            const i = steps.findIndex((s) => s.id === p.step.id);
            const next = i >= 0 ? steps.map((s, j) => (j === i ? p.step : s)) : [...steps, p.step];
            next.sort(byListOrder);
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
        <div className="empty" style={{ padding: 80 }}>{tr('加载中…', 'Loading…')}</div>
      </div>
    );
  }
  if (isError || !voyage) {
    const notFound = error instanceof ApiError && error.status === 404;
    return (
      <div className="page fadeup">
        <div className="card card-pad" style={{ textAlign: 'center', padding: 60 }}>
          <div style={{ fontSize: 15, fontWeight: 650, marginBottom: 8 }}>
            {notFound ? tr('任务不存在', 'Task not found') : tr('无法加载任务详情', 'Failed to load task detail')}
          </div>
          <div style={{ fontSize: 12.5, color: 'var(--text-3)', marginBottom: 18 }}>
            {error instanceof Error ? error.message : tr('后端不可用，请稍后重试', 'Backend unavailable — try again later')}
          </div>
          <div className="row gap8" style={{ justifyContent: 'center' }}>
            <button className="btn btn-soft" onClick={() => void refetch()}>{tr('重试', 'Retry')}</button>
            <button className="btn btn-ghost" onClick={() => navigate('/voyages')}>{tr('返回列表', 'Back to list')}</button>
          </div>
        </div>
      </div>
    );
  }

  const steps = [...(voyage.steps ?? [])].sort(byListOrder);
  const planEvents = voyage.plan_history ?? [];
  const entries = buildTimelineEntries(steps, planEvents);
  const totalTokens = steps.reduce((acc, s) => acc + (stepTokenCount(s.tokens) ?? 0), 0);
  const planAdjusted = (voyage.plan_iteration ?? 0) > 0;

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
            {planAdjusted && (
              <span
                className="pill sm"
                style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}
                title={tr('执行过程中计划被调整过（自动重试后的调整 / 按执行结果追加的轮次）', 'The plan was adjusted during execution (after auto-retries / extra rounds based on results)')}
              >
                {tr('计划调整', 'Plan adjusted')} ×{voyage.plan_iteration}
              </span>
            )}
            <span className="mono muted" style={{ fontSize: 11 }}>
              {tr('创建', 'Created')} {fmtTime(voyage.created_at)} · {tr('耗时', 'took')} {fmtDuration(voyage.created_at, active ? null : voyage.updated_at)}
            </span>
            {totalTokens > 0 && (
              <span className="mono muted" style={{ fontSize: 11 }}>· {fmtTokens(totalTokens)} tokens</span>
            )}
          </div>
        </div>
        {active && (
          <button className="btn btn-ghost" disabled={cancelMutation.isPending} onClick={() => cancelMutation.mutate()}>
            <Icon name="x" size={13} />
            {tr('取消任务', 'Cancel task')}
          </button>
        )}
        {voyage.kind === 'presentation' && voyage.status === 'done' && (
          <PresentationDownload voyageId={voyage.id} goal={voyage.goal} />
        )}
      </div>

      {/* 当前活动（循环感知：执行中的任务可能反复经过 执行→校验→调整计划） */}
      <div className="card card-pad" style={{ marginBottom: 20 }}>
        <ActivityBar
          voyage={voyage}
          steps={steps}
          onOpenGates={() => openGates(null)}
          onResume={() => resumeMutation.mutate()}
          resuming={resumeMutation.isPending}
        />
      </div>

      {/* 文献任务：本次同步结果汇总 */}
      {WIKI_RUN_KINDS.has(voyage.kind) && <WikiRunSummary steps={steps} />}

      {/* 本次任务使用的技能（启动时快照，中途改技能不影响） */}
      {(voyage.skills ?? []).length > 0 && (
        <div className="card card-pad" style={{ marginBottom: 20 }}>
          <div className="row gap8" style={{ flexWrap: 'wrap', alignItems: 'center' }}>
            <span style={{ fontSize: 12, color: 'var(--text-3)', flexShrink: 0 }}>{tr('本次任务使用的技能：', 'Skills used in this task:')}</span>
            {voyage.skills!.map((s) => (
              <span
                key={`${s.slug}-${s.target}`}
                className="pill sm"
                style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}
                title={`${s.slug} v${s.version} · ${s.target}`}
              >
                {s.name} v{s.version}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* 步骤时间线（任务板：清单序渲染，计划调整插入分隔条目，作废步骤可选显示） */}
      <div className="row" style={{ marginBottom: 12 }}>
        <span className="section-h">
          <Icon name="compass" size={15} style={{ color: 'var(--accent)' }} />
          {tr('步骤时间线', 'Steps')}
        </span>
        {planAdjusted && (
          <label
            className="row gap6"
            style={{ marginLeft: 'auto', fontSize: 12, color: 'var(--text-3)', cursor: 'pointer', userSelect: 'none' }}
          >
            <input
              type="checkbox"
              checked={showObsolete}
              onChange={(e) => setShowObsolete(e.target.checked)}
            />
            {tr('显示已作废步骤', 'Show obsolete steps')}
          </label>
        )}
      </div>
      {entries.length === 0 ? (
        <div className="card card-pad empty" style={{ padding: 40 }}>
          {tr('正在规划步骤…', 'Planning steps…')}
        </div>
      ) : (
        <Timeline>
          {entries.map((entry, i) => {
            const last = i === entries.length - 1;
            if (entry.kind === 'plan') {
              return (
                <TimelineItem
                  key={`plan-${entry.event.iteration}`}
                  marker={<Icon name="refresh" size={12} />}
                  markerBg="var(--accent-soft)"
                  markerColor="var(--accent-text)"
                  last={last}
                >
                  <PlanEventCard event={entry.event} />
                </TimelineItem>
              );
            }
            const m = stepMarker(entry.step);
            return (
              <TimelineItem key={entry.step.id} marker={entry.index} markerBg={m.bg} markerColor={m.color} last={last}>
                <StepCard step={entry.step} planEvents={planEvents} />
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
              {tr('实时日志', 'Live log')}
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
