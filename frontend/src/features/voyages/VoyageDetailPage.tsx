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
  type VoyageDetail,
  type VoyageStatus,
  type VoyageStepRead,
} from '../../lib/api';

/* ============================================================
   /voyages/:id — 航程详情：状态机进度条 + 步骤时间线 + SSE 实时。
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

// —— 状态机进度条 ——

const MACHINE = [
  { key: 'planning', zh: '规划', en: 'Planning' },
  { key: 'executing', zh: '执行', en: 'Executing' },
  { key: 'verifying', zh: '校验', en: 'Verifying' },
  { key: 'done', zh: '完成', en: 'Done' },
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

function MachineBar({ status, onOpenGates, onResume, resuming }: { status: VoyageStatus; onOpenGates: () => void; onResume?: () => void; resuming?: boolean }) {
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
                {tr(m.zh, m.en)}
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

function asObj(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
}

function num(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : 0;
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

// —— 步骤卡 ——

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
        {compact ? tr('原始数据', 'Raw data') : tr('观察', 'Observation')}
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

function StepCard({ step }: { step: VoyageStepRead }) {
  const obs = asObj(step.observation);
  const friendly = obs ? wikiStepFriendly(step.action, obs) : null;
  const obsolete = step.status === 'obsolete';
  return (
    <div className="card" style={{ padding: '14px 16px', opacity: obsolete ? 0.55 : 1 }}>
      <div className="row gap8" style={{ flexWrap: 'wrap' }}>
        <span style={{ fontSize: 13.5, fontWeight: 650, textDecoration: obsolete ? 'line-through' : 'none' }}>
          {step.title}
        </span>
        <span className="tag mono" style={{ fontSize: 10.5 }}>{step.action}</span>
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
      {step.verdict && !step.verdict.passed && step.verdict.reason && (
        <div style={{ marginTop: 8, fontSize: 12, color: 'var(--danger-tx)', lineHeight: 1.5 }}>
          {step.verdict.reason}
        </div>
      )}
      {friendly && <WikiStepSummary friendly={friendly} />}
      <ObservationBlock observation={step.observation} compact={!!friendly} />
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

      {/* 状态机进度条 */}
      <div className="card card-pad" style={{ marginBottom: 20 }}>
        <MachineBar status={voyage.status} onOpenGates={() => openGates(null)} onResume={() => resumeMutation.mutate()} resuming={resumeMutation.isPending} />
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

      {/* 步骤时间线（任务板：清单序渲染，作废步骤可选显示） */}
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
      {steps.length === 0 ? (
        <div className="card card-pad empty" style={{ padding: 40 }}>
          {tr('正在规划步骤…', 'Planning steps…')}
        </div>
      ) : (
        <Timeline>
          {steps.map((s, i) => {
            const m = stepMarker(s);
            return (
              <TimelineItem key={s.id} marker={i + 1} markerBg={m.bg} markerColor={m.color} last={i === steps.length - 1}>
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
